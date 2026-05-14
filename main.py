import os, io, uuid, json, math, httpx, ezdxf
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from minio.error import S3Error
from ezdxf.math import Vec2

app = FastAPI(title="PCB Panel AI API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── MinIO設定（從環境變數讀取）──
MINIO_ENDPOINT  = os.getenv("MINIO_ENDPOINT", "minio.zeabur.internal:9000")
MINIO_ACCESS    = os.getenv("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET    = os.getenv("MINIO_SECRET_KEY", "")
MINIO_SECURE    = os.getenv("MINIO_SECURE", "false").lower() == "true"
INPUT_BUCKET    = os.getenv("INPUT_BUCKET", "input-bucket")
OUTPUT_BUCKET   = os.getenv("OUTPUT_BUCKET", "output-bucket")

# ── Dify設定 ──
DIFY_API_URL    = os.getenv("DIFY_API_URL", "http://api.zeabur.internal:5001")
DIFY_API_KEY    = os.getenv("DIFY_API_KEY", "")
DIFY_WORKFLOW_ID= os.getenv("DIFY_WORKFLOW_ID", "")

def get_minio():
    return Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS,
                 secret_key=MINIO_SECRET, secure=MINIO_SECURE)

def ensure_bucket(client, bucket):
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except S3Error:
        pass

# ══════════════════════════════════════════
# DXF解析：讀取PCB外形，回傳長寬
# ══════════════════════════════════════════
def parse_dxf_dimensions(dxf_bytes: bytes) -> dict:
    doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8", errors="ignore")))
    msp = doc.modelspace()
    xs, ys = [], []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif t in ("LWPOLYLINE", "POLYLINE"):
                pts = list(e.get_points()) if t == "LWPOLYLINE" else list(e.points())
                for p in pts:
                    xs.append(p[0]); ys.append(p[1])
            elif t == "ARC":
                cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
                xs += [cx - r, cx + r]; ys += [cy - r, cy + r]
            elif t == "CIRCLE":
                cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
                xs += [cx - r, cx + r]; ys += [cy - r, cy + r]
            elif t == "SPLINE":
                for cp in e.control_points:
                    xs.append(cp[0]); ys.append(cp[1])
        except Exception:
            pass
    if not xs:
        raise ValueError("無法從DXF解析出PCB外形，請確認圖層內容")
    w = round(max(xs) - min(xs), 3)
    h = round(max(ys) - min(ys), 3)
    return {"length": max(w, h), "width": min(w, h),
            "x_min": min(xs), "y_min": min(ys),
            "x_max": max(xs), "y_max": max(ys)}

# ══════════════════════════════════════════
# 連版計算核心邏輯
# ══════════════════════════════════════════
RAIL_W  = 5.0   # 軌道板邊寬度 mm
SIDE_W  = 3.0   # 非軌道板邊寬度 mm
MAX_L   = 350.0 # 拼版最大長度
MAX_W   = 260.0 # 拼版最大寬度
GAP     = 2.0   # PCB間距

def calc_panel(pcb_l, pcb_w, rail_mode, thickness=1.6):
    """計算最佳連版方案，回傳完整設計參數"""
    # 板邊寬度決定
    if rail_mode == "軌道邊":
        top_rail = RAIL_W; bot_rail = RAIL_W
        left_rail = 0;     right_rail = 0
    elif rail_mode == "四周":
        top_rail = RAIL_W; bot_rail = RAIL_W
        left_rail = SIDE_W; right_rail = SIDE_W
    else:  # AI判斷
        top_rail = RAIL_W; bot_rail = RAIL_W
        left_rail = SIDE_W if pcb_w < 50 else 0
        right_rail = SIDE_W if pcb_w < 50 else 0

    # 計算最多能放幾片（X方向=長度方向）
    def max_fit(pcb_dim, rail_a, rail_b, max_dim):
        avail = max_dim - rail_a - rail_b
        n = int((avail + GAP) / (pcb_dim + GAP))
        return max(1, n)

    nx = max_fit(pcb_l, left_rail, right_rail, MAX_L)
    ny = max_fit(pcb_w, top_rail, bot_rail, MAX_W)

    # 限制最大片數
    while nx > 1 and (left_rail + right_rail + nx * pcb_l + (nx-1) * GAP) > MAX_L:
        nx -= 1
    while ny > 1 and (top_rail + bot_rail + ny * pcb_w + (ny-1) * GAP) > MAX_W:
        ny -= 1

    total_l = left_rail + right_rail + nx * pcb_l + (nx-1) * GAP
    total_w = top_rail + bot_rail + ny * pcb_w + (ny-1) * GAP

    # V-cut可行性判斷
    vcut_ok = (pcb_l > 0) and (thickness >= 0.8)

    return {
        "nx": nx, "ny": ny,
        "total_length": round(total_l, 2),
        "total_width":  round(total_w, 2),
        "top_rail": top_rail, "bot_rail": bot_rail,
        "left_rail": left_rail, "right_rail": right_rail,
        "gap": GAP,
        "vcut_ok": vcut_ok,
        "vcut_residual": 0.4,
        "vcut_angle": 30,
        "mark_diameter": 1.0,
        "mark_clearance": 2.0,
        "pcb_count": nx * ny,
    }

# ══════════════════════════════════════════
# DXF產生：畫出完整連版圖
# ══════════════════════════════════════════
def generate_panel_dxf(pcb_l, pcb_w, p: dict, product_name: str) -> bytes:
    doc = ezdxf.new(dxfversion="R2010")
    doc.header["$INSUNITS"] = 4  # mm

    # 建立圖層
    for layer, color in [
        ("OUTLINE", 7), ("BOARD_EDGE", 2), ("RAIL", 3),
        ("DIMENSION", 1), ("MARK", 6), ("VCUT", 4), ("TEXT", 7)
    ]:
        doc.layers.add(layer, color=color)

    msp = doc.modelspace()
    ox, oy = 0.0, 0.0  # 原點

    # ── 整版外框 ──
    tl = p["total_length"]; tw = p["total_width"]
    msp.add_lwpolyline(
        [(ox, oy), (ox+tl, oy), (ox+tl, oy+tw), (ox, oy+tw)],
        close=True, dxfattribs={"layer": "OUTLINE", "lineweight": 50}
    )

    # ── 板邊區域（用細線標示）──
    lr = p["left_rail"]; rr = p["right_rail"]
    tr = p["top_rail"];  br = p["bot_rail"]

    if lr > 0:
        msp.add_line((ox+lr, oy), (ox+lr, oy+tw), dxfattribs={"layer": "RAIL", "linetype": "DASHED"})
    if rr > 0:
        msp.add_line((ox+tl-rr, oy), (ox+tl-rr, oy+tw), dxfattribs={"layer": "RAIL", "linetype": "DASHED"})
    if br > 0:
        msp.add_line((ox, oy+br), (ox+tl, oy+br), dxfattribs={"layer": "RAIL", "linetype": "DASHED"})
    if tr > 0:
        msp.add_line((ox, oy+tw-tr), (ox+tl, oy+tw-tr), dxfattribs={"layer": "RAIL", "linetype": "DASHED"})

    # ── 各片PCB外形 ──
    for ix in range(p["nx"]):
        for iy in range(p["ny"]):
            px = ox + lr + ix * (pcb_l + p["gap"])
            py = oy + br + iy * (pcb_w + p["gap"])
            msp.add_lwpolyline(
                [(px, py), (px+pcb_l, py), (px+pcb_l, py+pcb_w), (px, py+pcb_w)],
                close=True, dxfattribs={"layer": "BOARD_EDGE", "lineweight": 35}
            )

    # ── V-cut線 ──
    if p["vcut_ok"]:
        # X方向V-cut（片與片之間）
        for ix in range(1, p["nx"]):
            vx = ox + lr + ix * pcb_l + (ix-1) * p["gap"]
            msp.add_line((ox, oy+br), (ox, oy+tw-tr),
                dxfattribs={"layer": "VCUT"})
            msp.add_line((vx, oy), (vx, oy+tw),
                dxfattribs={"layer": "VCUT", "lineweight": 25})
        # Y方向V-cut
        for iy in range(1, p["ny"]):
            vy = oy + br + iy * pcb_w + (iy-1) * p["gap"]
            msp.add_line((ox, vy), (ox+tl, vy),
                dxfattribs={"layer": "VCUT", "lineweight": 25})
        # 板邊V-cut線
        msp.add_line((ox+lr, oy), (ox+lr, oy+tw), dxfattribs={"layer": "VCUT"})
        msp.add_line((ox+tl-rr if rr>0 else ox+tl, oy),
                     (ox+tl-rr if rr>0 else ox+tl, oy+tw), dxfattribs={"layer": "VCUT"})

    # ── Mark點（整版3個，對角配置）──
    mark_r = p["mark_diameter"] / 2
    mark_clr = p["mark_clearance"]
    marks = [
        (ox + 2.5, oy + tw - 2.5),           # 左上
        (ox + tl - 2.5, oy + tw - 2.5),      # 右上
        (ox + 2.5, oy + 2.5),                # 左下
    ]
    for mx, my in marks:
        msp.add_circle((mx, my), mark_r, dxfattribs={"layer": "MARK"})
        msp.add_circle((mx, my), mark_r + mark_clr,
            dxfattribs={"layer": "MARK", "linetype": "DASHED"})

    # ── 尺寸標註 ──
    dim_y = oy - 8
    d1 = msp.add_linear_dim(base=(ox, dim_y), p1=(ox, oy), p2=(ox+tl, oy),
        dimstyle="EZDXF", dxfattribs={"layer": "DIMENSION"})
    d1.render()
    dim_x = ox - 8
    d2 = msp.add_linear_dim(base=(dim_x, oy), p1=(ox, oy), p2=(ox, oy+tw),
        angle=90, dimstyle="EZDXF", dxfattribs={"layer": "DIMENSION"})
    d2.render()

    # ── 標題文字 ──
    msp.add_text(
        f"{product_name}  {p['nx']}x{p['ny']}={p['pcb_count']}片  "
        f"{tl}x{tw}mm  V-cut {'✓' if p['vcut_ok'] else 'Tab'}",
        dxfattribs={"layer": "TEXT", "height": 3,
                    "insert": (ox, oy + tw + 5)}
    )

    buf = io.BytesIO()
    doc.write(buf)
    return buf.getvalue()

# ══════════════════════════════════════════
# 呼叫Dify Workflow取得AI分析
# ══════════════════════════════════════════
async def call_dify_workflow(inputs: dict) -> str:
    if not DIFY_API_KEY:
        return _fallback_analysis(inputs)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{DIFY_API_URL}/v1/workflows/run",
                headers={"Authorization": f"Bearer {DIFY_API_KEY}",
                         "Content-Type": "application/json"},
                json={"inputs": inputs, "response_mode": "blocking",
                      "user": "pcb-engineer"}
            )
            if r.status_code == 200:
                data = r.json()
                outputs = data.get("data", {}).get("outputs", {})
                return outputs.get("result", outputs.get("text",
                       json.dumps(outputs, ensure_ascii=False)))
    except Exception as e:
        pass
    return _fallback_analysis(inputs)

def _fallback_analysis(inputs: dict) -> str:
    p = inputs.get("panel_params", {})
    pcb_l = inputs.get("pcb_length", 0)
    pcb_w = inputs.get("pcb_width", 0)
    return f"""## PCB連版設計分析報告

### 基本規格
- 單板尺寸：{pcb_l} × {pcb_w} mm
- 板厚：{inputs.get('thickness', 1.6)} mm
- 板邊模式：{inputs.get('rail_mode', '軌道邊')}

### 連版方案
- 拼版配置：{p.get('nx',1)} × {p.get('ny',1)} = {p.get('pcb_count',1)} 片
- 拼版總尺寸：{p.get('total_length',0)} × {p.get('total_width',0)} mm
- 軌道板邊（上/下）：{p.get('top_rail',5)} mm
- 非軌道板邊（左/右）：{p.get('left_rail',0)} mm / {p.get('right_rail',0)} mm
- PCB間距：{p.get('gap',2)} mm

### 分板方式
{'**V-cut分板**（推薦）' if p.get('vcut_ok') else '**Tab/郵票孔分板**'}
{f"- V-cut角度：{p.get('vcut_angle',30)}°" if p.get('vcut_ok') else '- 外形有曲線，建議使用Tab分板'}
{f"- V-cut殘留厚度：{p.get('vcut_residual',0.4)} mm" if p.get('vcut_ok') else ''}

### Mark點配置
- 直徑：{p.get('mark_diameter',1.0)} mm（實心銅）
- 淨空區：{p.get('mark_clearance',2.0)} mm（無阻焊）
- 配置：整版3點對角配置（左上、右上、左下）

### 製程注意事項
1. **SMT方向**：長邊平行於傳送軌道方向，減少貼片偏移
2. **V-cut深度**：殘留厚度0.4mm，確保手動分板力道一致
3. **Mark點**：貼片前確認AOI相機能清晰辨識，避免油墨汙染
4. **板邊強度**：軌道板邊≥5mm，防止傳送過程板邊斷裂
5. **元件距板邊**：距V-cut線≥0.5mm，高腳元件≥1mm
6. **翹曲控制**：板厚{inputs.get('thickness',1.6)}mm，拼版後翹曲度需≤0.75%
7. **清洗注意**：波峰焊後清洗時，板邊治具孔位需對齊

### 治具建議
- SMT鋼板開口：依單板位置計算，建議共板開口
- 分板治具：V-cut刀具需對準V-cut線±0.1mm
"""

# ══════════════════════════════════════════
# API端點
# ══════════════════════════════════════════
@app.post("/api/analyze")
async def analyze_pcb(
    file: UploadFile = File(None),
    product_name: str = Form("PCB-001"),
    pcb_length: float = Form(0),
    pcb_width: float = Form(0),
    thickness: float = Form(1.6),
    rail_mode: str = Form("軌道邊"),
    process_type: str = Form("SMT"),
    max_panel_length: float = Form(350),
    max_panel_width: float = Form(260),
):
    try:
        minio = get_minio()
        ensure_bucket(minio, INPUT_BUCKET)
        ensure_bucket(minio, OUTPUT_BUCKET)

        # 1. 解析DXF或使用輸入尺寸
        dxf_key = None
        auto_detected = False
        if file and file.filename.endswith(".dxf"):
            content = await file.read()
            dxf_key = f"input/{uuid.uuid4()}_{file.filename}"
            minio.put_object(INPUT_BUCKET, dxf_key,
                io.BytesIO(content), len(content),
                content_type="application/dxf")
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
                    raise HTTPException(400, f"DXF解析失敗且未輸入尺寸：{e}")

        if pcb_length == 0 or pcb_width == 0:
            raise HTTPException(400, "請上傳DXF或手動輸入長度與寬度")

        # 2. 計算連版方案
        panel = calc_panel(pcb_length, pcb_width, rail_mode, thickness)

        # 3. 產生連版DXF
        dxf_bytes = generate_panel_dxf(pcb_length, pcb_width, panel, product_name)
        out_key = f"output/{uuid.uuid4()}_{product_name}_panel.dxf"
        minio.put_object(OUTPUT_BUCKET, out_key,
            io.BytesIO(dxf_bytes), len(dxf_bytes),
            content_type="application/dxf")

        # 4. 產生MinIO下載連結（7天有效）
        from datetime import timedelta
        download_url = minio.presigned_get_object(
            OUTPUT_BUCKET, out_key, expires=timedelta(days=7))

        # 5. 呼叫Dify取得AI分析
        ai_report = await call_dify_workflow({
            "product_name": product_name,
            "pcb_length": pcb_length,
            "pcb_width": pcb_width,
            "thickness": thickness,
            "rail_mode": rail_mode,
            "process_type": process_type,
            "panel_params": panel,
        })

        return JSONResponse({
            "success": True,
            "auto_detected": auto_detected,
            "pcb_length": pcb_length,
            "pcb_width": pcb_width,
            "thickness": thickness,
            "panel": panel,
            "download_url": download_url,
            "ai_report": ai_report,
            "dxf_key": out_key,
        })

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open("index.html").read())
