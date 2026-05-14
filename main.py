import os, io, uuid, json, tempfile, httpx, ezdxf
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from minio.error import S3Error
from datetime import timedelta

app = FastAPI(title="PCB Panel AI API")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── 環境變數 ──
MINIO_ENDPOINT  = os.getenv("MINIO_ENDPOINT",   "minio.zeabur.internal:9000")
MINIO_ACCESS    = os.getenv("MINIO_ACCESS_KEY",  "minio")
MINIO_SECRET    = os.getenv("MINIO_SECRET_KEY",  "")
MINIO_SECURE    = os.getenv("MINIO_SECURE",      "false").lower() == "true"
INPUT_BUCKET    = os.getenv("INPUT_BUCKET",      "input-bucket")
OUTPUT_BUCKET   = os.getenv("OUTPUT_BUCKET",     "output-bucket")
DIFY_API_URL    = os.getenv("DIFY_API_URL",      "")
DIFY_API_KEY    = os.getenv("DIFY_API_KEY",      "")

# 連版規範常數
RAIL_W  = 5.0    # 軌道板邊 mm
SIDE_W  = 3.0    # 側邊板邊 mm
MAX_L   = 350.0  # 最大拼版長度
MAX_W   = 260.0  # 最大拼版寬度
GAP     = 2.0    # PCB間距

# ══════════════════════════════════════════
# MinIO 工具函式
# ══════════════════════════════════════════
def get_minio():
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS,
        secret_key=MINIO_SECRET,
        secure=MINIO_SECURE
    )

def ensure_bucket(client: Minio, bucket: str):
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception:
        pass

def upload_bytes(client: Minio, bucket: str, key: str,
                 data: bytes, content_type: str = "application/octet-stream"):
    client.put_object(
        bucket, key,
        io.BytesIO(data), len(data),
        content_type=content_type
    )

def get_download_url(client: Minio, bucket: str, key: str) -> str:
    try:
        url = client.presigned_get_object(
            bucket, key, expires=timedelta(days=7))
        return url
    except Exception:
        return ""

# ══════════════════════════════════════════
# DXF 解析：讀取PCB外形回傳長寬
# ══════════════════════════════════════════
def parse_dxf_dimensions(dxf_bytes: bytes) -> dict:
    # 先嘗試記憶體讀取，失敗則寫臨時檔
    tmp_path = None
    try:
        text = dxf_bytes.decode("utf-8", errors="ignore")
        doc = ezdxf.read(io.StringIO(text))
    except Exception:
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".dxf", delete=False, mode="wb") as f:
                f.write(dxf_bytes)
                tmp_path = f.name
            doc = ezdxf.readfile(tmp_path)
        except Exception as e:
            raise ValueError(f"DXF格式無法解析：{e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    msp = doc.modelspace()
    xs, ys = [], []

    for entity in msp:
        t = entity.dxftype()
        try:
            if t == "LINE":
                xs += [entity.dxf.start.x, entity.dxf.end.x]
                ys += [entity.dxf.start.y, entity.dxf.end.y]
            elif t == "LWPOLYLINE":
                for pt in entity.get_points():
                    xs.append(float(pt[0]))
                    ys.append(float(pt[1]))
            elif t == "POLYLINE":
                for v in entity.vertices:
                    xs.append(float(v.dxf.location.x))
                    ys.append(float(v.dxf.location.y))
            elif t == "ARC":
                cx = entity.dxf.center.x
                cy = entity.dxf.center.y
                r  = entity.dxf.radius
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
            elif t == "CIRCLE":
                cx = entity.dxf.center.x
                cy = entity.dxf.center.y
                r  = entity.dxf.radius
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
            elif t == "SPLINE":
                for cp in entity.control_points:
                    xs.append(float(cp[0]))
                    ys.append(float(cp[1]))
            elif t == "ELLIPSE":
                cx = entity.dxf.center.x
                cy = entity.dxf.center.y
                rx = entity.dxf.major_axis.magnitude
                ry = rx * entity.dxf.ratio
                xs += [cx - rx, cx + rx]
                ys += [cy - ry, cy + ry]
        except Exception:
            continue

    if not xs or not ys:
        raise ValueError("DXF中找不到可辨識的PCB外形圖元，請確認圖層內容")

    w = round(max(xs) - min(xs), 3)
    h = round(max(ys) - min(ys), 3)
    length = max(w, h)
    width  = min(w, h)

    return {
        "length": length,
        "width":  width,
        "x_min":  round(min(xs), 3),
        "y_min":  round(min(ys), 3),
        "x_max":  round(max(xs), 3),
        "y_max":  round(max(ys), 3),
    }

# ══════════════════════════════════════════
# 連版計算核心
# ══════════════════════════════════════════
def calc_panel(pcb_l: float, pcb_w: float,
               rail_mode: str, thickness: float = 1.6,
               max_l: float = MAX_L, max_w: float = MAX_W) -> dict:

    # 板邊寬度
    if rail_mode == "軌道邊":
        top = RAIL_W; bot = RAIL_W; left = 0.0; right = 0.0
    elif rail_mode == "四周":
        top = RAIL_W; bot = RAIL_W; left = SIDE_W; right = SIDE_W
    else:  # AI判斷
        top = RAIL_W; bot = RAIL_W
        # 小板或波峰焊加側邊
        left  = SIDE_W if (pcb_w < 60 or thickness >= 1.6) else 0.0
        right = left

    # 計算最大片數
    def max_fit(dim, ra, rb, limit):
        avail = limit - ra - rb
        n = max(1, int((avail + GAP) / (dim + GAP)))
        # 驗證不超界
        while n > 1 and (ra + rb + n * dim + (n - 1) * GAP) > limit:
            n -= 1
        return n

    nx = max_fit(pcb_l, left, right, max_l)
    ny = max_fit(pcb_w, top,  bot,   max_w)

    total_l = round(left + right + nx * pcb_l + (nx - 1) * GAP, 2)
    total_w = round(top  + bot   + ny * pcb_w + (ny - 1) * GAP, 2)

    # V-cut 可行性（外形需為矩形且厚度≥0.8mm）
    vcut_ok = thickness >= 0.8

    return {
        "nx": nx, "ny": ny,
        "pcb_count":    nx * ny,
        "total_length": total_l,
        "total_width":  total_w,
        "top_rail":     top,
        "bot_rail":     bot,
        "left_rail":    left,
        "right_rail":   right,
        "gap":          GAP,
        "vcut_ok":      vcut_ok,
        "vcut_angle":   30,
        "vcut_residual":0.4,
        "mark_diameter":1.0,
        "mark_clearance":2.0,
    }

# ══════════════════════════════════════════
# DXF 產生：畫出完整連版圖
# ══════════════════════════════════════════
def generate_panel_dxf(pcb_l: float, pcb_w: float,
                       p: dict, product_name: str) -> bytes:
    doc = ezdxf.new(dxfversion="R2010")
    doc.header["$INSUNITS"] = 4  # mm

    # 圖層定義
    layers = {
        "OUTLINE":   7,   # 白：整版外框
        "BOARD_EDGE":2,   # 黃：單片PCB外框
        "RAIL":      3,   # 綠：板邊區域
        "VCUT":      4,   # 青：V-cut線
        "MARK":      6,   # 洋紅：Mark點
        "DIMENSION": 1,   # 紅：尺寸標註
        "TEXT":      7,   # 白：文字
    }
    for name, color in layers.items():
        doc.layers.add(name, color=color)

    msp  = doc.modelspace()
    ox, oy = 0.0, 0.0
    tl = p["total_length"]
    tw = p["total_width"]
    lr = p["left_rail"];  rr = p["right_rail"]
    tr = p["top_rail"];   br = p["bot_rail"]

    # ── 整版外框 ──
    msp.add_lwpolyline(
        [(ox, oy), (ox+tl, oy), (ox+tl, oy+tw), (ox, oy+tw)],
        close=True,
        dxfattribs={"layer": "OUTLINE", "lineweight": 50}
    )

    # ── 板邊虛線 ──
    def dashed_line(x1, y1, x2, y2):
        msp.add_line((x1, y1), (x2, y2),
            dxfattribs={"layer": "RAIL", "lineweight": 13})

    if lr > 0: dashed_line(ox+lr,    oy, ox+lr,    oy+tw)
    if rr > 0: dashed_line(ox+tl-rr, oy, ox+tl-rr, oy+tw)
    if br > 0: dashed_line(ox, oy+br,    ox+tl, oy+br)
    if tr > 0: dashed_line(ox, oy+tw-tr, ox+tl, oy+tw-tr)

    # ── 各片PCB外形 ──
    for ix in range(p["nx"]):
        for iy in range(p["ny"]):
            px = ox + lr + ix * (pcb_l + p["gap"])
            py = oy + br + iy * (pcb_w + p["gap"])
            msp.add_lwpolyline(
                [(px, py), (px+pcb_l, py),
                 (px+pcb_l, py+pcb_w), (px, py+pcb_w)],
                close=True,
                dxfattribs={"layer": "BOARD_EDGE", "lineweight": 35}
            )

    # ── V-cut 線 ──
    if p["vcut_ok"]:
        # 片與片之間 X方向
        for ix in range(1, p["nx"]):
            vx = ox + lr + ix * (pcb_l + p["gap"]) - p["gap"] / 2
            msp.add_line((vx, oy), (vx, oy+tw),
                dxfattribs={"layer": "VCUT", "lineweight": 25})
        # 片與片之間 Y方向
        for iy in range(1, p["ny"]):
            vy = oy + br + iy * (pcb_w + p["gap"]) - p["gap"] / 2
            msp.add_line((ox, vy), (ox+tl, vy),
                dxfattribs={"layer": "VCUT", "lineweight": 25})
        # 板邊 V-cut 線
        if lr > 0:
            msp.add_line((ox+lr, oy), (ox+lr, oy+tw),
                dxfattribs={"layer": "VCUT", "lineweight": 18})
        if rr > 0:
            msp.add_line((ox+tl-rr, oy), (ox+tl-rr, oy+tw),
                dxfattribs={"layer": "VCUT", "lineweight": 18})
        if br > 0:
            msp.add_line((ox, oy+br), (ox+tl, oy+br),
                dxfattribs={"layer": "VCUT", "lineweight": 18})
        if tr > 0:
            msp.add_line((ox, oy+tw-tr), (ox+tl, oy+tw-tr),
                dxfattribs={"layer": "VCUT", "lineweight": 18})

    # ── Mark點（整版3點對角配置）──
    mr  = p["mark_diameter"] / 2
    mc  = p["mark_clearance"]
    marks = [
        (ox + 2.5,      oy + tw - 2.5),
        (ox + tl - 2.5, oy + tw - 2.5),
        (ox + 2.5,      oy + 2.5),
    ]
    for mx, my in marks:
        msp.add_circle((mx, my), mr,
            dxfattribs={"layer": "MARK"})
        msp.add_circle((mx, my), mr + mc,
            dxfattribs={"layer": "MARK", "lineweight": 9})

    # ── 尺寸標註 ──
    try:
        d1 = msp.add_linear_dim(
            base=(ox, oy - 10),
            p1=(ox, oy), p2=(ox + tl, oy),
            dimstyle="EZDXF",
            dxfattribs={"layer": "DIMENSION"}
        )
        d1.render()
        d2 = msp.add_linear_dim(
            base=(ox - 10, oy),
            p1=(ox, oy), p2=(ox, oy + tw),
            angle=90,
            dimstyle="EZDXF",
            dxfattribs={"layer": "DIMENSION"}
        )
        d2.render()
    except Exception:
        pass

    # ── 標題文字 ──
    vcut_str = f"V-cut {p['vcut_angle']}°" if p["vcut_ok"] else "Tab分板"
    msp.add_text(
        f"{product_name}  {p['nx']}x{p['ny']}={p['pcb_count']}片  "
        f"{tl}x{tw}mm  {vcut_str}",
        dxfattribs={
            "layer":  "TEXT",
            "height": max(2.5, tl * 0.012),
            "insert": (ox, oy + tw + 4)
        }
    )

    buf = io.BytesIO()
    doc.write(buf)
    return buf.getvalue()

# ══════════════════════════════════════════
# Dify Workflow 呼叫
# ══════════════════════════════════════════
async def call_dify(inputs: dict) -> str:
    if not DIFY_API_KEY or not DIFY_API_URL:
        return _builtin_report(inputs)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{DIFY_API_URL}/v1/workflows/run",
                headers={
                    "Authorization": f"Bearer {DIFY_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "inputs": inputs,
                    "response_mode": "blocking",
                    "user": "pcb-engineer"
                }
            )
            if r.status_code == 200:
                data = r.json()
                out  = data.get("data", {}).get("outputs", {})
                return (out.get("result")
                        or out.get("text")
                        or json.dumps(out, ensure_ascii=False))
    except Exception:
        pass
    return _builtin_report(inputs)

def _builtin_report(inputs: dict) -> str:
    p  = inputs.get("panel_params", {})
    pl = inputs.get("pcb_length", 0)
    pw = inputs.get("pcb_width",  0)
    pt = inputs.get("thickness",  1.6)
    rm = inputs.get("rail_mode",  "軌道邊")
    pc = inputs.get("process_type", "SMT")

    vcut_lines = ""
    if p.get("vcut_ok"):
        vcut_lines = (
            f"- 分板方式：**V-cut**（推薦）\n"
            f"- V-cut 角度：{p.get('vcut_angle', 30)}°\n"
            f"- V-cut 殘留厚度：{p.get('vcut_residual', 0.4)} mm\n"
        )
    else:
        vcut_lines = "- 分板方式：**Tab / 郵票孔**（板厚<0.8mm或外形有曲線）\n"

    return f"""## PCB 連版設計分析報告

### 單板規格
- 尺寸：{pl} × {pw} mm　板厚：{pt} mm
- 製程：{pc}　板邊模式：{rm}

### 連版方案
- 拼版配置：**{p.get('nx',1)} × {p.get('ny',1)} = {p.get('pcb_count',1)} 片**
- 拼版總尺寸：**{p.get('total_length',0)} × {p.get('total_width',0)} mm**
- 軌道板邊（上/下）：{p.get('top_rail',5)} mm
- 側邊板邊（左/右）：{p.get('left_rail',0)} / {p.get('right_rail',0)} mm
- PCB 間距：{p.get('gap',2)} mm

### 分板方式
{vcut_lines}
### Fiducial Mark 配置
- 直徑：{p.get('mark_diameter',1.0)} mm（實心銅，無阻焊）
- 淨空區半徑：{p.get('mark_clearance',2.0)} mm
- 配置：整版 3 點對角（左上、右上、左下）

### 製程注意事項
1. **PCB方向**：長邊平行軌道傳送方向，降低貼片偏移風險
2. **V-cut 深度**：殘留 0.4 mm，分板時施力需均勻，避免 PCB 彎曲損傷元件
3. **元件距 V-cut 線**：一般元件 ≥ 0.5 mm，高腳元件 ≥ 1.0 mm
4. **Mark 點維護**：貼片前確認 Mark 點無氧化、無錫渣覆蓋，確保 AOI 辨識率
5. **板邊強度**：軌道板邊 ≥ 5 mm，防止 SMT 傳送過程斷裂
6. **翹曲控制**：板厚 {pt} mm，拼版後翹曲度需 ≤ 0.75 %（IPC-7711 標準）
7. **清洗注意**：波峰焊後清洗時，確認治具夾持不覆蓋板邊 V-cut 線
8. **鋼板開口**：SMT 鋼板建議共板開口，減少換板時間

### 材料利用率估算
- 單板面積：{round(pl * pw, 1)} mm²
- 拼版總面積：{round(p.get('total_length',0) * p.get('total_width',0), 1)} mm²
- 利用率：{round(pl * pw * p.get('pcb_count',1) /
    max(p.get('total_length',1) * p.get('total_width',1), 1) * 100, 1)} %
"""

# ══════════════════════════════════════════
# API 端點
# ══════════════════════════════════════════
@app.get("/health")
def health():
    return {"status": "ok",
            "minio": MINIO_ENDPOINT,
            "dify":  bool(DIFY_API_KEY)}

@app.post("/api/analyze")
async def analyze_pcb(
    file:             UploadFile = File(None),
    product_name:     str   = Form("PCB-001"),
    pcb_length:       float = Form(0),
    pcb_width:        float = Form(0),
    thickness:        float = Form(1.6),
    rail_mode:        str   = Form("軌道邊"),
    process_type:     str   = Form("SMT"),
    max_panel_length: float = Form(350),
    max_panel_width:  float = Form(260),
):
    try:
        # ── MinIO 初始化 ──
        mc = get_minio()
        ensure_bucket(mc, INPUT_BUCKET)
        ensure_bucket(mc, OUTPUT_BUCKET)

        # ── 解析 DXF 或使用手動輸入 ──
        auto_detected = False
        dxf_key = None

        if file is not None and file.filename and file.filename.endswith(".dxf"):
            content = await file.read()
            if len(content) > 0:
                # 上傳原始 DXF 到 MinIO
                dxf_key = f"input/{uuid.uuid4()}_{file.filename}"
                upload_bytes(mc, INPUT_BUCKET, dxf_key,
                             content, "application/dxf")
                # 嘗試自動偵測尺寸
                try:
                    dims = parse_dxf_dimensions(content)
                    if pcb_length == 0:
                        pcb_length = dims["length"]
                        auto_detected = True
                    if pcb_width == 0:
                        pcb_width = dims["width"]
                        auto_detected = True
                except Exception as e:
                    if pcb_length == 0 or pcb_width == 0:
                        raise HTTPException(400,
                            f"DXF解析失敗，請手動輸入尺寸（錯誤：{e}）")

        if pcb_length <= 0 or pcb_width <= 0:
            raise HTTPException(400,
                "請上傳 DXF 檔案或手動輸入單板長度與寬度（需大於 0）")

        # ── 計算連版方案 ──
        panel = calc_panel(
            pcb_length, pcb_width, rail_mode, thickness,
            max_panel_length, max_panel_width
        )

        # ── 產生連版 DXF ──
        dxf_bytes = generate_panel_dxf(
            pcb_length, pcb_width, panel, product_name)
        out_key = f"output/{uuid.uuid4()}_{product_name}_panel.dxf"
        upload_bytes(mc, OUTPUT_BUCKET, out_key,
                     dxf_bytes, "application/dxf")
        download_url = get_download_url(mc, OUTPUT_BUCKET, out_key)

        # ── 呼叫 Dify 取得 AI 報告 ──
        ai_report = await call_dify({
            "product_name":  product_name,
            "pcb_length":    pcb_length,
            "pcb_width":     pcb_width,
            "thickness":     thickness,
            "rail_mode":     rail_mode,
            "process_type":  process_type,
            "panel_params":  panel,
        })

        return JSONResponse({
            "success":       True,
            "auto_detected": auto_detected,
            "pcb_length":    pcb_length,
            "pcb_width":     pcb_width,
            "thickness":     thickness,
            "panel":         panel,
            "download_url":  download_url,
            "ai_report":     ai_report,
        })

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"系統錯誤：{str(e)}")

@app.get("/", response_class=HTMLResponse)
def index():
    try:
        return HTMLResponse(open("index.html", encoding="utf-8").read())
    except FileNotFoundError:
        return HTMLResponse("<h1>index.html not found</h1>", status_code=500)
