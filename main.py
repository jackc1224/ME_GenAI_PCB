import os, io, uuid, json, tempfile, httpx, ezdxf
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from datetime import timedelta

app = FastAPI(title="PCB Panel AI API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT",  "minio.zeabur.internal:9000")
MINIO_ACCESS   = os.getenv("MINIO_ACCESS_KEY", "minio")
MINIO_SECRET   = os.getenv("MINIO_SECRET_KEY", "")
MINIO_SECURE   = os.getenv("MINIO_SECURE",     "false").lower() == "true"
INPUT_BUCKET   = os.getenv("INPUT_BUCKET",     "input-bucket")
OUTPUT_BUCKET  = os.getenv("OUTPUT_BUCKET",    "output-bucket")
DIFY_API_URL   = os.getenv("DIFY_API_URL",     "")
DIFY_API_KEY   = os.getenv("DIFY_API_KEY",     "")
RAIL_W=5.0; SIDE_W=3.0; MAX_L=350.0; MAX_W=260.0; GAP=2.0

def get_mc():
    return Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS, secret_key=MINIO_SECRET, secure=MINIO_SECURE)

def ensure_bucket(mc, b):
    try:
        if not mc.bucket_exists(b): mc.make_bucket(b)
    except Exception: pass

def minio_put(mc, bucket, key, data: bytes, ct="application/octet-stream"):
    mc.put_object(bucket, key, io.BytesIO(data), length=len(data), content_type=ct)

def minio_url(mc, bucket, key):
    try: return mc.presigned_get_object(bucket, key, expires=timedelta(days=7))
    except Exception: return ""

def parse_dxf(b: bytes) -> dict:
    tmp = None
    try:
        doc = ezdxf.read(io.StringIO(b.decode("utf-8", errors="ignore")))
    except Exception:
        try:
            with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False, mode="wb") as f:
                f.write(b); tmp = f.name
            doc = ezdxf.readfile(tmp)
        except Exception as e:
            raise ValueError(f"DXF無法解析：{e}")
        finally:
            if tmp and os.path.exists(tmp): os.unlink(tmp)
    xs, ys = [], []
    for e in doc.modelspace():
        t = e.dxftype()
        try:
            if t == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]; ys += [e.dxf.start.y, e.dxf.end.y]
            elif t == "LWPOLYLINE":
                for p in e.get_points(): xs.append(float(p[0])); ys.append(float(p[1]))
            elif t == "POLYLINE":
                for v in e.vertices: xs.append(float(v.dxf.location.x)); ys.append(float(v.dxf.location.y))
            elif t in ("ARC","CIRCLE"):
                cx,cy,r = e.dxf.center.x,e.dxf.center.y,e.dxf.radius
                xs+=[cx-r,cx+r]; ys+=[cy-r,cy+r]
            elif t == "SPLINE":
                for cp in e.control_points: xs.append(float(cp[0])); ys.append(float(cp[1]))
        except Exception: continue
    if not xs: raise ValueError("DXF中找不到PCB外形圖元")
    w=round(max(xs)-min(xs),3); h=round(max(ys)-min(ys),3)
    return {"length":max(w,h),"width":min(w,h),"x_min":min(xs),"y_min":min(ys),"x_max":max(xs),"y_max":max(ys)}

def dxf_to_svg(b: bytes) -> str:
    import math
    try: doc = ezdxf.read(io.StringIO(b.decode("utf-8", errors="ignore")))
    except Exception: return ""
    xs,ys,items = [],[],[]
    for e in doc.modelspace():
        t = e.dxftype()
        try:
            if t=="LINE":
                x1,y1,x2,y2 = e.dxf.start.x,e.dxf.start.y,e.dxf.end.x,e.dxf.end.y
                xs+=[x1,x2]; ys+=[y1,y2]; items.append(("L",x1,y1,x2,y2))
            elif t=="LWPOLYLINE":
                pts=list(e.get_points())
                for i in range(len(pts)-1):
                    x1,y1,x2,y2=pts[i][0],pts[i][1],pts[i+1][0],pts[i+1][1]
                    xs+=[x1,x2]; ys+=[y1,y2]; items.append(("L",x1,y1,x2,y2))
                if e.is_closed and len(pts)>1: items.append(("L",pts[-1][0],pts[-1][1],pts[0][0],pts[0][1]))
            elif t=="CIRCLE":
                cx,cy,r=e.dxf.center.x,e.dxf.center.y,e.dxf.radius
                xs+=[cx-r,cx+r]; ys+=[cy-r,cy+r]; items.append(("C",cx,cy,r))
            elif t=="ARC":
                cx,cy,r=e.dxf.center.x,e.dxf.center.y,e.dxf.radius
                xs+=[cx-r,cx+r]; ys+=[cy-r,cy+r]
                items.append(("A",cx,cy,r,e.dxf.start_angle,e.dxf.end_angle))
        except Exception: continue
    if not xs: return ""
    minx,miny,maxx,maxy=min(xs),min(ys),max(xs),max(ys)
    W=maxx-minx or 1; H=maxy-miny or 1; vw=vh=280; m=20
    s=min((vw-m*2)/W,(vh-m*2)/H)
    ox_=m+(vw-m*2-W*s)/2; oy_=m+(vh-m*2-H*s)/2
    tx=lambda x: round(ox_+(x-minx)*s,2)
    ty=lambda y: round(oy_+(maxy-y)*s,2)
    els=[]
    for it in items:
        if it[0]=="L": els.append(f'<line x1="{tx(it[1])}" y1="{ty(it[2])}" x2="{tx(it[3])}" y2="{ty(it[4])}" stroke="#38c6a0" stroke-width="1.5" fill="none"/>')
        elif it[0]=="C": els.append(f'<circle cx="{tx(it[1])}" cy="{ty(it[2])}" r="{round(it[3]*s,2)}" stroke="#38c6a0" stroke-width="1.5" fill="none"/>')
        elif it[0]=="A":
            _,cx,cy,r,sa,ea=it
            x1=cx+r*math.cos(math.radians(sa)); y1=cy+r*math.sin(math.radians(sa))
            x2=cx+r*math.cos(math.radians(ea)); y2=cy+r*math.sin(math.radians(ea))
            lg=1 if (ea-sa)%360>180 else 0
            els.append(f'<path d="M{tx(x1)} {ty(y1)} A{round(r*s,2)} {round(r*s,2)} 0 {lg} 0 {tx(x2)} {ty(y2)}" stroke="#38c6a0" stroke-width="1.5" fill="none"/>')
    return f'<svg viewBox="0 0 {vw} {vh}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">{"".join(els)}</svg>'

def calc_panel(pcb_l,pcb_w,rail_mode,thickness=1.6,has_bga=False,is_irr=False,has_fin=False,has_tall=False,max_l=MAX_L,max_w=MAX_W):
    if rail_mode=="軌道邊": top=RAIL_W;bot=RAIL_W;left=0.0;right=0.0
    elif rail_mode=="四周": top=RAIL_W;bot=RAIL_W;left=SIDE_W;right=SIDE_W
    else:
        top=RAIL_W;bot=RAIL_W
        left=SIDE_W if (pcb_w<60 or has_tall) else 0.0; right=left
        if has_fin: right=0.0
    def fit(dim,ra,rb,lim):
        n=max(1,int((lim-ra-rb+GAP)/(dim+GAP)))
        while n>1 and (ra+rb+n*dim+(n-1)*GAP)>lim: n-=1
        return n
    nx=fit(pcb_l,left,right,max_l); ny=fit(pcb_w,top,bot,max_w)
    tl=round(left+right+nx*pcb_l+(nx-1)*GAP,2); tw=round(top+bot+ny*pcb_w+(ny-1)*GAP,2)
    return {"nx":nx,"ny":ny,"pcb_count":nx*ny,"total_length":tl,"total_width":tw,
            "top_rail":top,"bot_rail":bot,"left_rail":left,"right_rail":right,"gap":GAP,
            "vcut_ok":thickness>=0.8 and not is_irr,"vcut_angle":30,"vcut_residual":0.4,
            "mark_diameter":1.0,"mark_clearance":2.0}

def gen_dxf(pcb_l,pcb_w,p,name):
    doc=ezdxf.new(dxfversion="R2010"); doc.header["$INSUNITS"]=4
    for ln,col in [("OUTLINE",7),("BOARD_EDGE",2),("RAIL",3),("VCUT",4),("MARK",6),("DIMENSION",1),("TEXT",7)]:
        doc.layers.add(ln,color=col)
    msp=doc.modelspace(); ox=oy=0.0
    tl=p["total_length"]; tw=p["total_width"]; lr=p["left_rail"]; rr=p["right_rail"]; tr=p["top_rail"]; br=p["bot_rail"]
    msp.add_lwpolyline([(ox,oy),(ox+tl,oy),(ox+tl,oy+tw),(ox,oy+tw)],close=True,dxfattribs={"layer":"OUTLINE","lineweight":50})
    def dl(x1,y1,x2,y2): msp.add_line((x1,y1),(x2,y2),dxfattribs={"layer":"RAIL","lineweight":13})
    if lr>0: dl(ox+lr,oy,ox+lr,oy+tw)
    if rr>0: dl(ox+tl-rr,oy,ox+tl-rr,oy+tw)
    if br>0: dl(ox,oy+br,ox+tl,oy+br)
    if tr>0: dl(ox,oy+tw-tr,ox+tl,oy+tw-tr)
    for ix in range(p["nx"]):
        for iy in range(p["ny"]):
            px=ox+lr+ix*(pcb_l+p["gap"]); py=oy+br+iy*(pcb_w+p["gap"])
            msp.add_lwpolyline([(px,py),(px+pcb_l,py),(px+pcb_l,py+pcb_w),(px,py+pcb_w)],close=True,dxfattribs={"layer":"BOARD_EDGE","lineweight":35})
    if p["vcut_ok"]:
        for ix in range(1,p["nx"]):
            vx=ox+lr+ix*(pcb_l+p["gap"])-p["gap"]/2
            msp.add_line((vx,oy),(vx,oy+tw),dxfattribs={"layer":"VCUT","lineweight":25})
        for iy in range(1,p["ny"]):
            vy=oy+br+iy*(pcb_w+p["gap"])-p["gap"]/2
            msp.add_line((ox,vy),(ox+tl,vy),dxfattribs={"layer":"VCUT","lineweight":25})
        for x in ([ox+lr] if lr>0 else [])+([ox+tl-rr] if rr>0 else []):
            msp.add_line((x,oy),(x,oy+tw),dxfattribs={"layer":"VCUT","lineweight":18})
        for y in ([oy+br] if br>0 else [])+([oy+tw-tr] if tr>0 else []):
            msp.add_line((ox,y),(ox+tl,y),dxfattribs={"layer":"VCUT","lineweight":18})
    for mx,my in [(ox+2.5,oy+tw-2.5),(ox+tl-2.5,oy+tw-2.5),(ox+2.5,oy+2.5)]:
        msp.add_circle((mx,my),p["mark_diameter"]/2,dxfattribs={"layer":"MARK"})
        msp.add_circle((mx,my),p["mark_diameter"]/2+p["mark_clearance"],dxfattribs={"layer":"MARK","lineweight":9})
    try:
        d1=msp.add_linear_dim(base=(ox,oy-10),p1=(ox,oy),p2=(ox+tl,oy),dimstyle="EZDXF",dxfattribs={"layer":"DIMENSION"}); d1.render()
        d2=msp.add_linear_dim(base=(ox-10,oy),p1=(ox,oy),p2=(ox,oy+tw),angle=90,dimstyle="EZDXF",dxfattribs={"layer":"DIMENSION"}); d2.render()
    except Exception: pass
    vs="V-cut {}°".format(p["vcut_angle"]) if p["vcut_ok"] else "Tab分板"
    msp.add_text("{} {}x{}={}片 {}x{}mm {}".format(name,p["nx"],p["ny"],p["pcb_count"],tl,tw,vs),
        dxfattribs={"layer":"TEXT","height":max(2.5,tl*0.012),"insert":(ox,oy+tw+4)})
    buf=io.BytesIO(); doc.write(buf); return buf.getvalue()

def panel_svg(pcb_l,pcb_w,p):
    tl=p["total_length"]; tw=p["total_width"]; vw=vh=340; m=30
    s=min((vw-m*2)/tl,(vh-m*2)/tw)
    ox_=m+(vw-m*2-tl*s)/2; oy_=m+(vh-m*2-tw*s)/2
    tx=lambda x: round(ox_+x*s,2); ty=lambda y: round(oy_+(tw-y)*s,2)
    lr=p["left_rail"]; rr=p["right_rail"]; tr=p["top_rail"]; br=p["bot_rail"]
    els=[]
    els.append(f'<rect x="{tx(0)}" y="{ty(tw)}" width="{round(tl*s,2)}" height="{round(tw*s,2)}" fill="#0d1f0d" stroke="#38c6a0" stroke-width="2" rx="3"/>')
    for (rx_,ry_,rw_,rh_) in ([(tx(0),ty(tw),round(lr*s,2),round(tw*s,2))] if lr>0 else [])+\
        ([(tx(tl-rr),ty(tw),round(rr*s,2),round(tw*s,2))] if rr>0 else [])+\
        ([(tx(0),ty(br),round(tl*s,2),round(br*s,2))] if br>0 else [])+\
        ([(tx(0),ty(tw),round(tl*s,2),round(tr*s,2))] if tr>0 else []):
        els.append(f'<rect x="{rx_}" y="{ry_}" width="{rw_}" height="{rh_}" fill="rgba(56,198,160,0.15)" stroke="none"/>')
    for ix in range(p["nx"]):
        for iy in range(p["ny"]):
            px_=lr+ix*(pcb_l+p["gap"]); py_=br+iy*(pcb_w+p["gap"])
            els.append(f'<rect x="{tx(px_)}" y="{ty(py_+pcb_w)}" width="{round(pcb_l*s,2)}" height="{round(pcb_w*s,2)}" fill="rgba(79,142,247,0.18)" stroke="#4f8ef7" stroke-width="1.2" rx="1"/>')
    if p["vcut_ok"]:
        for ix in range(1,p["nx"]):
            vx=lr+ix*(pcb_l+p["gap"])-p["gap"]/2
            els.append(f'<line x1="{tx(vx)}" y1="{ty(tw)}" x2="{tx(vx)}" y2="{ty(0)}" stroke="#f0a732" stroke-width="1.2" stroke-dasharray="5,3"/>')
        for iy in range(1,p["ny"]):
            vy=br+iy*(pcb_w+p["gap"])-p["gap"]/2
            els.append(f'<line x1="{tx(0)}" y1="{ty(vy)}" x2="{tx(tl)}" y2="{ty(vy)}" stroke="#f0a732" stroke-width="1.2" stroke-dasharray="5,3"/>')
    for mx,my in [(2.5,tw-2.5),(tl-2.5,tw-2.5),(2.5,2.5)]:
        r=p["mark_diameter"]/2*s
        els.append(f'<circle cx="{tx(mx)}" cy="{ty(my)}" r="{round(r,2)}" fill="#e05c5c"/>')
        els.append(f'<circle cx="{tx(mx)}" cy="{ty(my)}" r="{round(r+p[\"mark_clearance\"]*s,2)}" fill="none" stroke="#e05c5c" stroke-width="0.8" stroke-dasharray="3,2"/>')
    els.append(f'<text x="{tx(tl/2)}" y="{ty(tw)-10}" text-anchor="middle" font-size="11" fill="#7a82a0" font-family="Arial">{tl}mm</text>')
    els.append(f'<text x="{tx(0)-10}" y="{ty(tw/2)}" text-anchor="middle" font-size="11" fill="#7a82a0" font-family="Arial" transform="rotate(-90,{tx(0)-10},{ty(tw/2)})">{tw}mm</text>')
    vs_label="V-cut" if p["vcut_ok"] else "Tab"
    els.append(f'<text x="{tx(tl/2)}" y="{ty(0)+18}" text-anchor="middle" font-size="10" fill="#4f8ef7" font-family="Arial">{p["nx"]}×{p["ny"]}={p["pcb_count"]}片　{vs_label}</text>')
    return f'<svg viewBox="0 0 {vw} {vh}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">{"".join(els)}</svg>'

async def call_dify(inp):
    if not DIFY_API_KEY or not DIFY_API_URL: return _report(inp)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r=await c.post(f"{DIFY_API_URL}/v1/workflows/run",
                headers={"Authorization":f"Bearer {DIFY_API_KEY}","Content-Type":"application/json"},
                json={"inputs":inp,"response_mode":"blocking","user":"pcb-engineer"})
            if r.status_code==200:
                out=r.json().get("data",{}).get("outputs",{})
                return out.get("result") or out.get("text") or json.dumps(out,ensure_ascii=False)
    except Exception: pass
    return _report(inp)

def _report(inp):
    p=inp.get("panel_params",{}); pl=inp.get("pcb_length",0); pw=inp.get("pcb_width",0)
    pt=inp.get("thickness",1.6); rm=inp.get("rail_mode","軌道邊"); pc=inp.get("process_type","SMT")
    vs=f"**V-cut**（{p.get('vcut_angle',30)}°，殘留{p.get('vcut_residual',0.4)}mm）" if p.get("vcut_ok") else "**Tab/郵票孔**"
    util=round(pl*pw*p.get("pcb_count",1)/max(p.get("total_length",1)*p.get("total_width",1),1)*100,1)
    special=[]
    if inp.get("has_bga"): special.append("- **BGA/QFN**：局部Mark點精度需±0.025mm")
    if inp.get("is_irregular"): special.append("- **異形板**：強制Tab分板，Tab寬2.0mm，郵票孔ø0.8mm")
    if inp.get("has_finger"): special.append("- **金手指**：連接器邊不加板邊，分板後需去毛邊")
    if inp.get("has_tall"): special.append("- **突出零件**：建議四周板邊，波峰焊治具需挖空")
    sp="\n".join(special) if special else "- 無特殊限制"
    return f"""## PCB 連版設計分析報告

### 單板規格
- 尺寸：{pl} × {pw} mm　板厚：{pt} mm　製程：{pc}　板邊：{rm}
- BGA/QFN：{"是" if inp.get("has_bga") else "否"}　異形板：{"是" if inp.get("is_irregular") else "否"}　金手指：{"是" if inp.get("has_finger") else "否"}　突出零件：{"是" if inp.get("has_tall") else "否"}

### 連版方案
- 拼版：**{p.get("nx",1)} × {p.get("ny",1)} = {p.get("pcb_count",1)} 片**
- 尺寸：**{p.get("total_length",0)} × {p.get("total_width",0)} mm**
- 軌道板邊：{p.get("top_rail",5)} mm　側邊板邊：{p.get("left_rail",0)} mm
- 間距：{p.get("gap",2)} mm　材料利用率：{util} %

### 分板方式：{vs}

### 特殊注意
{sp}

### 製程注意事項
1. 長邊平行軌道方向，降低貼片偏移
2. V-cut殘留0.4mm，分板施力均勻
3. 元件距V-cut線：一般≥0.5mm，高腳≥1.0mm
4. Mark點確認無氧化、無錫渣
5. 翹曲度需≤0.75%（IPC-7711）
6. SMT鋼板建議共板開口
7. 波峰焊清洗確認治具不覆蓋V-cut線
"""

@app.get("/health")
def health(): return {"status":"ok"}

@app.post("/api/analyze")
async def analyze(
    file:UploadFile=File(None), product_name:str=Form("PCB-001"),
    pcb_length:float=Form(0), pcb_width:float=Form(0), thickness:float=Form(1.6),
    rail_mode:str=Form("軌道邊"), process_type:str=Form("SMT"),
    has_bga:str=Form("N"), is_irregular:str=Form("N"),
    has_finger:str=Form("N"), has_tall:str=Form("N"),
    max_panel_length:float=Form(350), max_panel_width:float=Form(260),
):
    try:
        mc=get_mc(); ensure_bucket(mc,INPUT_BUCKET); ensure_bucket(mc,OUTPUT_BUCKET)
        auto=False; orig_svg=""
        if file and file.filename and file.filename.lower().endswith(".dxf"):
            content=await file.read()
            if content:
                key=f"input/{uuid.uuid4()}_{file.filename}"
                minio_put(mc,INPUT_BUCKET,key,content,"application/dxf")
                try:
                    dims=parse_dxf(content)
                    if pcb_length==0: pcb_length=dims["length"]; auto=True
                    if pcb_width==0: pcb_width=dims["width"]; auto=True
                    orig_svg=dxf_to_svg(content)
                except Exception as e:
                    if pcb_length==0 or pcb_width==0:
                        raise HTTPException(400,f"DXF解析失敗，請手動輸入尺寸：{e}")
        if pcb_length<=0 or pcb_width<=0:
            raise HTTPException(400,"請上傳DXF或手動輸入長度與寬度（需大於0）")
        bga=has_bga.upper()=="Y"; irr=is_irregular.upper()=="Y"
        fin=has_finger.upper()=="Y"; tal=has_tall.upper()=="Y"
        panel=calc_panel(pcb_length,pcb_width,rail_mode,thickness,bga,irr,fin,tal,max_panel_length,max_panel_width)
        dxf_out=gen_dxf(pcb_length,pcb_width,panel,product_name)
        out_key=f"output/{uuid.uuid4()}_{product_name}_panel.dxf"
        minio_put(mc,OUTPUT_BUCKET,out_key,dxf_out,"application/dxf")
        dl_url=minio_url(mc,OUTPUT_BUCKET,out_key)
        ps=panel_svg(pcb_length,pcb_width,panel)
        report=await call_dify({"product_name":product_name,"pcb_length":pcb_length,"pcb_width":pcb_width,
            "thickness":thickness,"rail_mode":rail_mode,"process_type":process_type,
            "has_bga":bga,"is_irregular":irr,"has_finger":fin,"has_tall":tal,"panel_params":panel})
        return JSONResponse({"success":True,"auto_detected":auto,"pcb_length":pcb_length,"pcb_width":pcb_width,
            "panel":panel,"download_url":dl_url,"ai_report":report,"orig_svg":orig_svg,"panel_svg":ps})
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,f"系統錯誤：{e}")

@app.get("/",response_class=HTMLResponse)
def index():
    try: return HTMLResponse(open("index.html",encoding="utf-8").read())
    except: return HTMLResponse("<h1>index.html not found</h1>",status_code=500)
