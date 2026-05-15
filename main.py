import os, io, uuid, json, tempfile, math, httpx, ezdxf
from ezdxf.math import Matrix44
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from datetime import timedelta

app = FastAPI(title="PCB Panel AI API - Real DXF Panelization")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio.zeabur.internal:9000")
MINIO_ACCESS = os.getenv("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET = os.getenv("MINIO_SECRET_KEY", "")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
INPUT_BUCKET = os.getenv("INPUT_BUCKET", "input-bucket")
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET", "output-bucket")
DIFY_API_URL = os.getenv("DIFY_API_URL", "")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")

RAIL_W = 5.0
SIDE_W = 3.0
MAX_L = 350.0
MAX_W = 260.0
GAP = 2.0


# ══════════════════════════════════════════
# MinIO
# ══════════════════════════════════════════
def get_mc():
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS,
        secret_key=MINIO_SECRET,
        secure=MINIO_SECURE,
    )


def ensure_bucket(mc, b):
    try:
        if not mc.bucket_exists(b):
            mc.make_bucket(b)
    except Exception:
        pass


def minio_put(mc, bucket, key, data: bytes, ct="application/octet-stream"):
    if isinstance(data, str):
        data = data.encode("utf-8")
    mc.put_object(bucket, key, io.BytesIO(data), length=len(data), content_type=ct)


def minio_url(mc, bucket, key):
    try:
        return mc.presigned_get_object(bucket, key, expires=timedelta(days=7))
    except Exception:
        return ""


# ══════════════════════════════════════════
# DXF 讀取與解析
# ══════════════════════════════════════════
def read_dxf_doc_from_bytes(b: bytes):
    """
    穩定讀取 DXF：
    1. 先嘗試以 StringIO 讀文字 DXF。
    2. 若失敗，寫成暫存 .dxf，再用 ezdxf.readfile() 讀取。
    """
    tmp = None
    try:
        return ezdxf.read(io.StringIO(b.decode("utf-8", errors="ignore")))
    except Exception:
        pass

    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False, mode="wb") as f:
            f.write(b)
            tmp = f.name
        return ezdxf.readfile(tmp)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def collect_entity_points(doc):
    """
    掃描 modelspace，取得可用圖元點位。
    用於 bounding box 與 SVG 預覽。
    """
    xs, ys, items = [], [], []

    for e in doc.modelspace():
        t = e.dxftype()

        try:
            if t == "LINE":
                x1 = float(e.dxf.start.x)
                y1 = float(e.dxf.start.y)
                x2 = float(e.dxf.end.x)
                y2 = float(e.dxf.end.y)
                xs += [x1, x2]
                ys += [y1, y2]
                items.append(("L", x1, y1, x2, y2))

            elif t == "LWPOLYLINE":
                pts = list(e.get_points())

                for i in range(len(pts) - 1):
                    x1 = float(pts[i][0])
                    y1 = float(pts[i][1])
                    x2 = float(pts[i + 1][0])
                    y2 = float(pts[i + 1][1])
                    xs += [x1, x2]
                    ys += [y1, y2]
                    items.append(("L", x1, y1, x2, y2))

                if e.is_closed and len(pts) > 1:
                    x1 = float(pts[-1][0])
                    y1 = float(pts[-1][1])
                    x2 = float(pts[0][0])
                    y2 = float(pts[0][1])
                    xs += [x1, x2]
                    ys += [y1, y2]
                    items.append(("L", x1, y1, x2, y2))

            elif t == "POLYLINE":
                pts = []
                for v in e.vertices:
                    x = float(v.dxf.location.x)
                    y = float(v.dxf.location.y)
                    pts.append((x, y))
                    xs.append(x)
                    ys.append(y)

                for i in range(len(pts) - 1):
                    x1, y1 = pts[i]
                    x2, y2 = pts[i + 1]
                    items.append(("L", x1, y1, x2, y2))

                if getattr(e, "is_closed", False) and len(pts) > 1:
                    x1, y1 = pts[-1]
                    x2, y2 = pts[0]
                    items.append(("L", x1, y1, x2, y2))

            elif t == "CIRCLE":
                cx = float(e.dxf.center.x)
                cy = float(e.dxf.center.y)
                r = float(e.dxf.radius)
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
                items.append(("C", cx, cy, r))

            elif t == "ARC":
                cx = float(e.dxf.center.x)
                cy = float(e.dxf.center.y)
                r = float(e.dxf.radius)
                sa = float(e.dxf.start_angle)
                ea = float(e.dxf.end_angle)
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
                items.append(("A", cx, cy, r, sa, ea))

            elif t == "SPLINE":
                cps = []
                try:
                    cps = list(e.control_points)
                except Exception:
                    cps = []

                for cp in cps:
                    x = float(cp[0])
                    y = float(cp[1])
                    xs.append(x)
                    ys.append(y)

        except Exception:
            continue

    return xs, ys, items


def parse_dxf(b: bytes) -> dict:
    """
    解析原始 DXF 尺寸。
    回傳 length/width 給前端與排版計算，同時保留 bbox 實際方向給真實 DXF 複製使用。
    """
    try:
        doc = read_dxf_doc_from_bytes(b)
    except Exception as e:
        raise ValueError("DXF無法解析：" + str(e))

    xs, ys, _ = collect_entity_points(doc)

    if not xs or not ys:
        raise ValueError("DXF中找不到PCB外形圖元")

    x_min = min(xs)
    y_min = min(ys)
    x_max = max(xs)
    y_max = max(ys)

    bbox_w = round(x_max - x_min, 3)
    bbox_h = round(y_max - y_min, 3)

    return {
        "length": max(bbox_w, bbox_h),
        "width": min(bbox_w, bbox_h),
        "bbox_width": bbox_w,
        "bbox_height": bbox_h,
        "x_min": x_min,
        "y_min": y_min,
        "x_max": x_max,
        "y_max": y_max,
    }


def dxf_to_svg(b: bytes) -> str:
    """
    將上傳的 DXF 轉成 SVG 預覽。
    """
    try:
        doc = read_dxf_doc_from_bytes(b)
    except Exception:
        return ""

    xs, ys, items = collect_entity_points(doc)

    if not xs or not ys or not items:
        return ""

    minx, miny = min(xs), min(ys)
    maxx, maxy = max(xs), max(ys)

    W = maxx - minx
    H = maxy - miny

    if W <= 0 or H <= 0:
        return ""

    vw = 520
    vh = 260
    m = 18

    s = min((vw - m * 2) / W, (vh - m * 2) / H)
    ox_ = m + (vw - m * 2 - W * s) / 2
    oy_ = m + (vh - m * 2 - H * s) / 2

    def tx(x):
        return round(ox_ + (x - minx) * s, 2)

    def ty(y):
        return round(oy_ + (maxy - y) * s, 2)

    els = []
    els.append('<rect x="0" y="0" width="{}" height="{}" fill="#0b0f18"/>'.format(vw, vh))

    for it in items:
        if it[0] == "L":
            _, x1, y1, x2, y2 = it
            els.append(
                '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="#f7e600" stroke-width="1.2" fill="none"/>'.format(
                    tx(x1), ty(y1), tx(x2), ty(y2)
                )
            )

        elif it[0] == "C":
            _, cx, cy, r = it
            els.append(
                '<circle cx="{}" cy="{}" r="{}" stroke="#ff66cc" stroke-width="1.1" fill="none"/>'.format(
                    tx(cx), ty(cy), round(r * s, 2)
                )
            )

        elif it[0] == "A":
            _, cx, cy, r, sa, ea = it
            x1 = cx + r * math.cos(math.radians(sa))
            y1 = cy + r * math.sin(math.radians(sa))
            x2 = cx + r * math.cos(math.radians(ea))
            y2 = cy + r * math.sin(math.radians(ea))

            large_arc = 1 if (ea - sa) % 360 > 180 else 0

            els.append(
                '<path d="M{} {} A{} {} 0 {} 0 {} {}" stroke="#ff66cc" stroke-width="1.1" fill="none"/>'.format(
                    tx(x1), ty(y1), round(r * s, 2), round(r * s, 2), large_arc, tx(x2), ty(y2)
                )
            )

    els.append(
        '<rect x="{}" y="{}" width="{}" height="{}" stroke="#334155" stroke-width="1" fill="none" rx="4"/>'.format(
            m / 2, m / 2, vw - m, vh - m
        )
    )

    return (
        '<svg viewBox="0 0 {} {}" xmlns="http://www.w3.org/2000/svg" '
        'style="width:100%;height:100%;display:block">{}</svg>'
    ).format(vw, vh, "".join(els))


# ══════════════════════════════════════════
# 連版計算
# ══════════════════════════════════════════
def calc_panel(pcb_l, pcb_w, rail_mode, thickness=1.6,
               has_bga=False, is_irr=False, has_fin=False, has_tall=False,
               max_l=MAX_L, max_w=MAX_W):
    """
    連板計算：
    - GAP 固定至少 2 mm。
    - 依設備最大尺寸計算最大可行 nx / ny。
    """
    if rail_mode == "軌道邊":
        top = RAIL_W
        bot = RAIL_W
        left = 0.0
        right = 0.0
    elif rail_mode == "四周":
        top = RAIL_W
        bot = RAIL_W
        left = SIDE_W
        right = SIDE_W
    else:
        top = RAIL_W
        bot = RAIL_W
        left = SIDE_W if (pcb_w < 60 or has_tall) else 0.0
        right = left
        if has_fin:
            right = 0.0

    def fit(dim, ra, rb, lim):
        n = max(1, int((lim - ra - rb + GAP) / (dim + GAP)))
        while n > 1 and (ra + rb + n * dim + (n - 1) * GAP) > lim:
            n -= 1
        return n

    nx = fit(pcb_l, left, right, max_l)
    ny = fit(pcb_w, top, bot, max_w)

    tl = round(left + right + nx * pcb_l + (nx - 1) * GAP, 2)
    tw = round(top + bot + ny * pcb_w + (ny - 1) * GAP, 2)

    return {
        "nx": nx,
        "ny": ny,
        "pcb_count": nx * ny,
        "total_length": tl,
        "total_width": tw,
        "top_rail": top,
        "bot_rail": bot,
        "left_rail": left,
        "right_rail": right,
        "gap": GAP,
        "vcut_ok": thickness >= 0.8 and not is_irr,
        "vcut_angle": 30,
        "vcut_residual": 0.4,
        "mark_diameter": 1.0,
        "mark_clearance": 2.0,
    }


# ══════════════════════════════════════════
# DXF 產生：真實複製原始 DXF 圖元，不是模擬矩形
# ══════════════════════════════════════════
def ensure_layer(doc, name, color=7):
    try:
        if name not in doc.layers:
            doc.layers.add(name=name, color=color)
    except Exception:
        pass


def add_rect(msp, x, y, w, h, layer, lineweight=13):
    msp.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)],
        close=True,
        dxfattribs={"layer": layer, "lineweight": lineweight},
    )


def transform_entity(entity, matrix):
    try:
        entity.transform(matrix)
        return True
    except Exception:
        return False


def add_actual_pcb_entities(target_doc, source_doc, source_bbox, target_x, target_y, target_w, target_h):
    """
    將原始 DXF 的 modelspace 實體複製到指定位置。
    這是重點：產出的連版圖使用原始 DXF 實際圖元，而不是模擬矩形。
    """
    target_msp = target_doc.modelspace()
    source_msp = source_doc.modelspace()

    x_min = float(source_bbox["x_min"])
    y_min = float(source_bbox["y_min"])
    bbox_w = max(float(source_bbox["bbox_width"]), 0.0001)
    bbox_h = max(float(source_bbox["bbox_height"]), 0.0001)

    sx = target_w / bbox_w
    sy = target_h / bbox_h

    matrix = Matrix44.chain(
        Matrix44.translate(-x_min, -y_min, 0),
        Matrix44.scale(sx, sy, 1),
        Matrix44.translate(target_x, target_y, 0),
    )

    for e in list(source_msp):
        try:
            copied = e.copy()

            # 將原始 PCB 本體統一放在 PCB_ORIGINAL layer，方便在 CAD 中辨識。
            try:
                copied.dxf.layer = "PCB_ORIGINAL"
            except Exception:
                pass

            if transform_entity(copied, matrix):
                target_msp.add_entity(copied)

        except Exception:
            continue


def gen_dxf(pcb_l, pcb_w, p, name, source_bytes=None, source_bbox=None):
    """
    產生連版 DXF。

    若有 source_bytes：
      - 讀入原始 DXF
      - 依照 nx / ny 實際複製原始 DXF 圖元
      - 加上板邊、V-cut / Tab、Mark、尺寸與文字

    若沒有 source_bytes：
      - 才使用矩形模擬。
    """
    doc = ezdxf.new(dxfversion="R2010")
    doc.header["$INSUNITS"] = 4

    layer_defs = [
        ("OUTLINE", 7),
        ("PCB_ORIGINAL", 2),
        ("BOARD_EDGE", 2),
        ("RAIL", 7),
        ("VCUT", 4),
        ("ROUTE_TAB", 7),
        ("MARK", 6),
        ("DIMENSION", 1),
        ("TEXT", 7),
    ]

    for ln, col in layer_defs:
        ensure_layer(doc, ln, col)

    msp = doc.modelspace()

    ox = 0.0
    oy = 0.0

    tl = p["total_length"]
    tw = p["total_width"]
    lr = p["left_rail"]
    rr = p["right_rail"]
    tr = p["top_rail"]
    br = p["bot_rail"]
    gap = max(float(p.get("gap", GAP)), 2.0)

    # 外框
    add_rect(msp, ox, oy, tl, tw, "OUTLINE", 50)

    # 工藝邊線
    def dl(x1, y1, x2, y2, layer="RAIL", lw=18):
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer, "lineweight": lw})

    if lr > 0:
        dl(ox + lr, oy, ox + lr, oy + tw)
    if rr > 0:
        dl(ox + tl - rr, oy, ox + tl - rr, oy + tw)
    if br > 0:
        dl(ox, oy + br, ox + tl, oy + br)
    if tr > 0:
        dl(ox, oy + tw - tr, ox + tl, oy + tw - tr)

    source_doc = None
    if source_bytes:
        try:
            source_doc = read_dxf_doc_from_bytes(source_bytes)
        except Exception:
            source_doc = None

    if source_doc and source_bbox:
        # 真實複製原始 DXF 圖元
        for ix in range(p["nx"]):
            for iy in range(p["ny"]):
                px = ox + lr + ix * (pcb_l + gap)
                py = oy + br + iy * (pcb_w + gap)

                add_actual_pcb_entities(
                    target_doc=doc,
                    source_doc=source_doc,
                    source_bbox=source_bbox,
                    target_x=px,
                    target_y=py,
                    target_w=pcb_l,
                    target_h=pcb_w,
                )

                # 輔助板框：白色/綠色 CAM 參考線
                add_rect(msp, px, py, pcb_l, pcb_w, "BOARD_EDGE", 13)

    else:
        # 沒有原始 DXF 時才使用矩形模擬
        for ix in range(p["nx"]):
            for iy in range(p["ny"]):
                px = ox + lr + ix * (pcb_l + gap)
                py = oy + br + iy * (pcb_w + gap)
                add_rect(msp, px, py, pcb_l, pcb_w, "BOARD_EDGE", 35)

    # V-cut 或 ROUTE/TAB
    if p["vcut_ok"]:
        for ix in range(1, p["nx"]):
            vx = ox + lr + ix * (pcb_l + gap) - gap / 2
            dl(vx, oy, vx, oy + tw, "VCUT", 25)

        for iy in range(1, p["ny"]):
            vy = oy + br + iy * (pcb_w + gap) - gap / 2
            dl(ox, vy, ox + tl, vy, "VCUT", 25)

    else:
        # Tab 模式：在板與板間畫連接點參考線，間距仍保留至少 2mm
        tab_w = 2.0
        tab_len = 7.0

        # 垂直板間
        for ix in range(1, p["nx"]):
            x_center = ox + lr + ix * pcb_l + (ix - 0.5) * gap

            for iy in range(p["ny"]):
                y0 = oy + br + iy * (pcb_w + gap)

                for ratio in (0.30, 0.70):
                    cy = y0 + pcb_w * ratio
                    add_rect(
                        msp,
                        x_center - tab_w / 2,
                        cy - tab_len / 2,
                        tab_w,
                        tab_len,
                        "ROUTE_TAB",
                        18,
                    )

        # 水平板間
        for iy in range(1, p["ny"]):
            y_center = oy + br + iy * pcb_w + (iy - 0.5) * gap

            for ix in range(p["nx"]):
                x0 = ox + lr + ix * (pcb_l + gap)

                for ratio in (0.30, 0.70):
                    cx = x0 + pcb_l * ratio
                    add_rect(
                        msp,
                        cx - tab_len / 2,
                        y_center - tab_w / 2,
                        tab_len,
                        tab_w,
                        "ROUTE_TAB",
                        18,
                    )

    # Mark 點
    mark_r = p["mark_diameter"] / 2
    mark_cl = p["mark_clearance"]

    for mx, my in [
        (ox + 2.5, oy + tw - 2.5),
        (ox + tl - 2.5, oy + tw - 2.5),
        (ox + 2.5, oy + 2.5),
    ]:
        msp.add_circle((mx, my), mark_r, dxfattribs={"layer": "MARK"})
        msp.add_circle((mx, my), mark_r + mark_cl, dxfattribs={"layer": "MARK", "lineweight": 9})

    # 尺寸
    try:
        d1 = msp.add_linear_dim(
            base=(ox, oy - 10),
            p1=(ox, oy),
            p2=(ox + tl, oy),
            dimstyle="EZDXF",
            dxfattribs={"layer": "DIMENSION"},
        )
        d1.render()

        d2 = msp.add_linear_dim(
            base=(ox - 10, oy),
            p1=(ox, oy),
            p2=(ox, oy + tw),
            angle=90,
            dimstyle="EZDXF",
            dxfattribs={"layer": "DIMENSION"},
        )
        d2.render()
    except Exception:
        pass

    vs = "V-cut {}deg".format(p["vcut_angle"]) if p["vcut_ok"] else "Router / Tab"

    label = "{} {}x{}={}pcs {}x{}mm {} | REAL SOURCE DXF PANELIZATION".format(
        name,
        p["nx"],
        p["ny"],
        p["pcb_count"],
        tl,
        tw,
        vs,
    )

    msp.add_text(
        label,
        dxfattribs={
            "layer": "TEXT",
            "height": max(2.5, tl * 0.012),
            "insert": (ox, oy + tw + 4),
        },
    )

    # ezdxf doc.write() 輸出 str，要 encode 成 bytes 才能上傳 MinIO
    text_buf = io.StringIO()
    doc.write(text_buf)
    return text_buf.getvalue().encode("utf-8")


# ══════════════════════════════════════════
# 連版 SVG 預覽
# ══════════════════════════════════════════
def panel_svg(pcb_l, pcb_w, p):
    tl = p["total_length"]
    tw = p["total_width"]
    vw = 340
    vh = 340
    m = 30

    s = min((vw - m * 2) / tl, (vh - m * 2) / tw)
    ox_ = m + (vw - m * 2 - tl * s) / 2
    oy_ = m + (vh - m * 2 - tw * s) / 2

    def tx(x):
        return round(ox_ + x * s, 2)

    def ty(y):
        return round(oy_ + (tw - y) * s, 2)

    lr = p["left_rail"]
    rr = p["right_rail"]
    tr = p["top_rail"]
    br = p["bot_rail"]
    mark_r = p["mark_diameter"] / 2
    mark_cl = p["mark_clearance"]

    els = []

    # 整版外框
    els.append(
        '<rect x="{}" y="{}" width="{}" height="{}" fill="#0d1f0d" stroke="#38c6a0" stroke-width="2" rx="3"/>'.format(
            tx(0), ty(tw), round(tl * s, 2), round(tw * s, 2)
        )
    )

    # 板邊區域
    rail_areas = []
    if lr > 0:
        rail_areas.append((tx(0), ty(tw), round(lr * s, 2), round(tw * s, 2)))
    if rr > 0:
        rail_areas.append((tx(tl - rr), ty(tw), round(rr * s, 2), round(tw * s, 2)))
    if br > 0:
        rail_areas.append((tx(0), ty(br), round(tl * s, 2), round(br * s, 2)))
    if tr > 0:
        rail_areas.append((tx(0), ty(tw), round(tl * s, 2), round(tr * s, 2)))

    for rx_, ry_, rw_, rh_ in rail_areas:
        els.append(
            '<rect x="{}" y="{}" width="{}" height="{}" fill="rgba(56,198,160,0.15)" stroke="none"/>'.format(
                rx_, ry_, rw_, rh_
            )
        )

    # 各片 PCB 預覽框
    for ix in range(p["nx"]):
        for iy in range(p["ny"]):
            px_ = lr + ix * (pcb_l + p["gap"])
            py_ = br + iy * (pcb_w + p["gap"])

            els.append(
                '<rect x="{}" y="{}" width="{}" height="{}" fill="rgba(79,142,247,0.18)" stroke="#4f8ef7" stroke-width="1.2" rx="1"/>'.format(
                    tx(px_), ty(py_ + pcb_w), round(pcb_l * s, 2), round(pcb_w * s, 2)
                )
            )

    # V-cut線
    if p["vcut_ok"]:
        for ix in range(1, p["nx"]):
            vx = lr + ix * (pcb_l + p["gap"]) - p["gap"] / 2
            els.append(
                '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="#f0a732" stroke-width="1.2" stroke-dasharray="5,3"/>'.format(
                    tx(vx), ty(tw), tx(vx), ty(0)
                )
            )
        for iy in range(1, p["ny"]):
            vy = br + iy * (pcb_w + p["gap"]) - p["gap"] / 2
            els.append(
                '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="#f0a732" stroke-width="1.2" stroke-dasharray="5,3"/>'.format(
                    tx(0), ty(vy), tx(tl), ty(vy)
                )
            )

    # Mark點
    for mx, my in [(2.5, tw - 2.5), (tl - 2.5, tw - 2.5), (2.5, 2.5)]:
        r_inner = round(mark_r * s, 2)
        r_outer = round((mark_r + mark_cl) * s, 2)

        els.append('<circle cx="{}" cy="{}" r="{}" fill="#e05c5c"/>'.format(tx(mx), ty(my), r_inner))
        els.append(
            '<circle cx="{}" cy="{}" r="{}" fill="none" stroke="#e05c5c" stroke-width="0.8" stroke-dasharray="3,2"/>'.format(
                tx(mx), ty(my), r_outer
            )
        )

    # 尺寸文字
    els.append(
        '<text x="{}" y="{}" text-anchor="middle" font-size="11" fill="#7a82a0" font-family="Arial">{}mm</text>'.format(
            tx(tl / 2), ty(tw) - 10, tl
        )
    )

    rot_x = tx(0) - 12
    rot_y = ty(tw / 2)

    els.append(
        '<text x="{}" y="{}" text-anchor="middle" font-size="11" fill="#7a82a0" font-family="Arial" transform="rotate(-90,{},{})">{}mm</text>'.format(
            rot_x, rot_y, rot_x, rot_y, tw
        )
    )

    vs_label = "V-cut" if p["vcut_ok"] else "Tab"

    els.append(
        '<text x="{}" y="{}" text-anchor="middle" font-size="10" fill="#4f8ef7" font-family="Arial">{}x{}={}pcs  {}</text>'.format(
            tx(tl / 2), ty(0) + 18, p["nx"], p["ny"], p["pcb_count"], vs_label
        )
    )

    return (
        '<svg viewBox="0 0 {} {}" xmlns="http://www.w3.org/2000/svg" '
        'style="width:100%;height:100%">{}</svg>'
    ).format(vw, vh, "".join(els))


# ══════════════════════════════════════════
# AI 報告
# ══════════════════════════════════════════
async def call_dify(inp):
    if not DIFY_API_KEY or not DIFY_API_URL:
        return _report(inp)

    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                DIFY_API_URL + "/v1/workflows/run",
                headers={
                    "Authorization": "Bearer " + DIFY_API_KEY,
                    "Content-Type": "application/json",
                },
                json={"inputs": inp, "response_mode": "blocking", "user": "pcb-engineer"},
            )

            if r.status_code == 200:
                out = r.json().get("data", {}).get("outputs", {})
                return out.get("result") or out.get("text") or json.dumps(out, ensure_ascii=False)

    except Exception:
        pass

    return _report(inp)


def _report(inp):
    p = inp.get("panel_params", {})
    pl = inp.get("pcb_length", 0)
    pw = inp.get("pcb_width", 0)
    pt = inp.get("thickness", 1.6)
    rm = inp.get("rail_mode", "軌道邊")
    pc = inp.get("process_type", "SMT")

    bga = "是" if inp.get("has_bga") else "否"
    irr = "是" if inp.get("is_irregular") else "否"
    fin = "是" if inp.get("has_finger") else "否"
    tal = "是" if inp.get("has_tall") else "否"

    vs = (
        "V-cut（{}°，殘留{}mm）".format(p.get("vcut_angle", 30), p.get("vcut_residual", 0.4))
        if p.get("vcut_ok")
        else "Tab/郵票孔"
    )

    tl = p.get("total_length", 0)
    tw2 = p.get("total_width", 0)
    util = round(pl * pw * p.get("pcb_count", 1) / max(tl * tw2, 1) * 100, 1)

    special = []
    if inp.get("has_bga"):
        special.append("- BGA/QFN：局部 Mark 點精度需 ±0.025mm")
    if inp.get("is_irregular"):
        special.append("- 異形板：建議 Router / Tab 分板，ROUTE 各連板間距至少 2mm 以上")
    if inp.get("has_finger"):
        special.append("- 金手指：連接器邊不加板邊，分板後需去毛邊")
    if inp.get("has_tall"):
        special.append("- 突出零件：建議四周板邊，波峰焊治具需挖空")

    sp = "\n".join(special) if special else "- 無特殊限制"

    return """## PCB 連版設計分析報告

### 單板規格
- 尺寸：{} × {} mm　板厚：{} mm　製程：{}　板邊：{}
- BGA/QFN：{}　異形板：{}　金手指：{}　突出零件：{}

### 連版方案
- 拼版：**{} × {} = {} 片**
- 尺寸：**{} × {} mm**
- 軌道板邊：{} mm　側邊板邊：{} mm
- 間距：{} mm　材料利用率：{} %

### 分板方式
- {}

### 真實 DXF 連版說明
- 本版 DXF 不是模擬矩形圖。
- 系統會讀取原始 DXF modelspace 圖元，依拼版數實際複製原始 PCB 幾何。
- 外框、板邊、V-cut / Router Tab、Mark 與尺寸線為系統新增輔助設計層。

### 特殊注意
{}

### 製程注意事項
1. 長邊平行軌道方向，降低貼片偏移。
2. ROUTE 各連板間距至少 2 mm 以上。
3. 元件距 V-cut 線：一般 ≥0.5mm，高腳 ≥1.0mm。
4. Mark 點確認無氧化、無錫渣。
5. 翹曲度需 ≤0.75%。
6. SMT 鋼板建議共板開口。
7. 正式投產前仍需 ME / CAM 工程師確認原始 Gerber / DXF 與 CAM 製程限制。
""".format(
        pl, pw, pt, pc, rm, bga, irr, fin, tal,
        p.get("nx", 1), p.get("ny", 1), p.get("pcb_count", 1),
        tl, tw2, p.get("top_rail", 5), p.get("left_rail", 0),
        p.get("gap", 2), util, vs, sp
    )


# ══════════════════════════════════════════
# API
# ══════════════════════════════════════════
@app.get("/health")
def health():
    return {"status": "ok", "version": "real-dxf-panelization-2026-05-05-logic"}


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(None),
    product_name: str = Form("PCB-001"),
    pcb_length: float = Form(0),
    pcb_width: float = Form(0),
    thickness: float = Form(1.6),
    rail_mode: str = Form("軌道邊"),
    process_type: str = Form("SMT"),
    has_bga: str = Form("N"),
    is_irregular: str = Form("N"),
    has_finger: str = Form("N"),
    has_tall: str = Form("N"),
    max_panel_length: float = Form(350),
    max_panel_width: float = Form(260),
):
    try:
        mc = get_mc()
        ensure_bucket(mc, INPUT_BUCKET)
        ensure_bucket(mc, OUTPUT_BUCKET)

        auto = False
        orig_svg = ""
        source_bytes = None
        source_bbox = None

        if file and file.filename and file.filename.lower().endswith(".dxf"):
            content = await file.read()

            if content:
                source_bytes = content
                key = "input/" + str(uuid.uuid4()) + "_" + file.filename
                minio_put(mc, INPUT_BUCKET, key, content, "application/dxf")

                try:
                    dims = parse_dxf(content)
                    source_bbox = dims

                    if pcb_length == 0:
                        pcb_length = dims["length"]
                        auto = True

                    if pcb_width == 0:
                        pcb_width = dims["width"]
                        auto = True

                    orig_svg = dxf_to_svg(content)

                except Exception as e:
                    if pcb_length == 0 or pcb_width == 0:
                        raise HTTPException(400, "DXF解析失敗，請手動輸入尺寸：" + str(e))

        if pcb_length <= 0 or pcb_width <= 0:
            raise HTTPException(400, "請上傳 DXF 或手動輸入長度與寬度（需大於0）")

        bga = has_bga.upper() == "Y"
        irr = is_irregular.upper() == "Y"
        fin = has_finger.upper() == "Y"
        tal = has_tall.upper() == "Y"

        panel = calc_panel(
            pcb_length,
            pcb_width,
            rail_mode,
            thickness,
            bga,
            irr,
            fin,
            tal,
            max_panel_length,
            max_panel_width,
        )

        dxf_out = gen_dxf(
            pcb_l=pcb_length,
            pcb_w=pcb_width,
            p=panel,
            name=product_name,
            source_bytes=source_bytes,
            source_bbox=source_bbox,
        )

        out_key = "output/" + str(uuid.uuid4()) + "_" + product_name + "_real_panel.dxf"
        minio_put(mc, OUTPUT_BUCKET, out_key, dxf_out, "application/dxf")
        dl_url = minio_url(mc, OUTPUT_BUCKET, out_key)

        ps = panel_svg(pcb_length, pcb_width, panel)

        report = await call_dify({
            "product_name": product_name,
            "pcb_length": pcb_length,
            "pcb_width": pcb_width,
            "thickness": thickness,
            "rail_mode": rail_mode,
            "process_type": process_type,
            "has_bga": bga,
            "is_irregular": irr,
            "has_finger": fin,
            "has_tall": tal,
            "panel_params": panel,
            "real_dxf_panelization": bool(source_bytes),
        })

        return JSONResponse({
            "success": True,
            "auto_detected": auto,
            "pcb_length": pcb_length,
            "pcb_width": pcb_width,
            "panel": panel,
            "download_url": dl_url,
            "ai_report": report,
            "orig_svg": orig_svg,
            "panel_svg": ps,
            "real_dxf_panelization": bool(source_bytes),
        })

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(500, "系統錯誤：" + str(e))


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        return HTMLResponse(open("index.html", encoding="utf-8").read())
    except Exception:
        return HTMLResponse("<h1>index.html not found</h1>", status_code=500)
