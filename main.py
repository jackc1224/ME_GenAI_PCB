from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional, Tuple
import os
import tempfile
import uuid
import shutil
from datetime import timedelta, datetime
import requests
import traceback
import json
import re
import io
import math

from minio import Minio
from minio.error import S3Error

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44


app = FastAPI(title="PCB Panelization API", version="0.9.4-me-template-detail-edge-routertab")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# 公司連板設計規範：程式內建規則庫
# =========================================================

PANEL_RULES = {
    "version": "company_panel_rules_v1.4_me_template_detail_edge_routertab",
    "source": "program_rule_base",

    "panel_size": {
        "absolute_max_length_mm": 510.0,
        "absolute_max_width_mm": 360.0,
        "juki_min_length_mm": 50.0,
        "juki_min_width_mm": 50.0,
        "juki_max_length_mm": 510.0,
        "juki_max_width_mm": 360.0,
        "smt_magazine_length_mm": 253.0,
        "smt_magazine_width_mm": 350.0,
    },

    "layout": {
        "candidate_patterns": [
            (1, 1), (1, 2), (2, 1), (2, 2), (3, 1),
            (1, 3), (3, 2), (2, 3), (4, 1), (1, 4),
        ],
        "preferred_aspect_ratio": 1.5,
        "near_square_min": 1.0,
        "near_square_max": 1.25,
        "caution_aspect_ratio": 3.0,
        "not_recommended_aspect_ratio": 4.0,
        "max_gap_between_boards_mm": 30.0,
    },

    "rail": {
        "company_recommended_min_mm": 8.0,
        "company_recommended_max_mm": 10.0,
        "default_mm": 8.0,
        "minimum_warning_mm": 4.0,
        "f_line_keepout_mm": 10.0,
        "four_side_required_when_tooling": True,
        "two_side_allowed_for_simple_vcut": True,
        "template_note": "依連板圖模板，若需 Tooling Hole / Fiducial / Router Tab，優先採四邊工藝邊；簡單 V-CUT 且無治具需求時才允許兩側工藝邊。",
    },

    "fiducial": {
        "panel_count": 3,
        "diameter_mm": 1.5,
        "strict_checklist_diameter_mm": 1.5,
        "clearance_mm": 5.0,
        "positions": ["top-left", "top-right", "bottom-left"],
        "internal_min_count": 2,
        "template_callout": "3-Ø1.5 Fiducial Mark",
    },

    "tooling_hole": {
        "count": 4,
        "diameter_mm": 3.05,
        "optional_jig_diameter_mm": 3.05,
        "clearance_mm": 5.0,
        "positions": ["bottom-left", "bottom-right", "top-left", "top-right"],
        "poka_yoke_offset_mm": 1.0,
        "template_callout": "4-Ø3.05 ±0.05 Tooling hole",
    },

    "tab": {
        "tab_length_mm": 7.0,
        "tab_width_mm": 2.0,
        "route_gap_mm": 2.0,
        "edge_tab_enabled": True,
        "edge_tab_count_per_board_edge": 2,
        "edge_tab_note": "板與板之間及板邊工藝邊皆規劃 Tab 連接點；連接點以白色線標示，需由 ME/CAM 確認分板應力。",
    },

    "dimension": {
        "leader_enabled": True,
        "leader_text_height": 1.8,
        "dimension_text_height": 1.8,
        "callout_text_height": 1.8,
        "dimension_offset_mm": 8.0,
        "dimension_stack_gap_mm": 6.0,
        "hole_position_reference_x_mm": 5.0,
        "hole_position_reference_y_mm": 10.0,
        "tooling_to_fiducial_distance_mm": 7.70,
        "tooling_callout_offset_x_mm": 9.0,
        "fiducial_callout_offset_y_mm": 5.0,
    },

    "white_marking": {
        "size_mm": "4.5 x 30 mm 或 5 x 30 mm",
        "count": 4,
        "description": "板邊正反面右側及對角需增加白框不塗滿 / 雷雕框，ODM 產品需特別確認。",
    },

    "cutting": {
        "vcut_component_distance_mm": 2.0,
        "vcut_height_check_distance_mm": 5.0,
        "vcut_max_height_within_5mm_mm": 10.0,
        "vcut_depth": "PCB 兩邊各 1/3",
        "vcut_angle_deg": 30,
        "router_min_gap_mm": 2.0,
        "router_tool_diameter_mm": 1.5,
        "connector_clearance_mm": 3.0,
        "mlcc_edge_distance_mm": 5.0,
        "router_high_part_rule": "ROUTE 正面朝上時，連接點 1 cm 內不得有 1.5 cm 高零件；1.5 cm 內不得有 3 cm 高零件。",
    },

    "risk_score": {
        "exceed_absolute_max": 1000,
        "below_min_size": 100,
        "exceed_smt_input": 100,
        "exceed_ict_input": 30,
        "near_square": 15,
        "aspect_gt_3": 25,
        "aspect_gt_4": 100,
        "irregular_shape": 15,
        "bga_qfn": 15,
        "dip": 10,
        "heavy_component": 10,
        "vcut_conflict": 100,
        "mlcc_near_edge_vcut": 100,
    },

    "check_items": [
        "Gerber 轉 DXF 尺寸比例是否為 1:1。",
        "單板尺寸與連板尺寸是否標示清楚。",
        "Panel 長寬是否超過最大 510 x 360 mm 或客戶 / 設備輸入限制。",
        "Panel 尺寸是否低於最小 50 x 50 mm。",
        "板內是否至少有 2 點光學點分布於對角。",
        "Panel 光學點與工具孔距離板邊是否至少 5 mm。",
        "板邊是否有 4 點固定點分布於對角並具防呆設計。",
        "板邊是否有 3 點光學點分布於對角並具防呆設計。",
        "板邊與連接點需以白色線規劃，並確認 Tab 位置與切點間距。",
        "是否加註 4.5 x 30 mm 或 5 x 30 mm 白框不塗滿 / 雷雕框。",
        "雙面零件排版是否有做讓位，零件側是否朝外避免上件干涉。",
        "零件是否重疊板邊，是否確認無干涉。",
        "MLCC 0805 以上距板邊 5 mm 以內時，是否改用 Router 製程。",
        "金手指周圍是否避免連接點與 V-CUT 線。",
        "排版設計 CONN 是否朝外側；RJ45 等 CONN 是否配合波峰焊方向。",
        "單板之間距離是否未超過 30 mm。",
        "是否避免陰陽板；若使用陰陽板，是否有註明原因。",
        "SMT F 線大尺寸 PCB 10 mm 內是否未放工具孔與光學點。",
        "V-CUT 線路徑是否無干涉零件。",
        "零件與 V-CUT 線距離是否大於 2 mm。",
        "V-CUT 線邊 5 mm 內零件高度是否小於等於 10 mm。",
        "V-CUT 深度是否為 PCB 兩邊各 1/3，開槽角度是否 30 度。",
        "ROUTE 切點間距是否至少 2 mm 以上。",
        "連接點是否取消郵票孔或 V-CUT 線。",
        "圓弧或斜線切點是否加註白線。",
        "連接點與凸出零件是否相距至少 3 mm。",
        "ROUTE 正面朝上設計是否符合高件距離限制。",
        "ODM EV/DV 階段可用郵票孔，MV 階段是否避免郵票孔。",
        "正式投產前是否由 ME / CAM 工程師確認。",
    ],
}


# =========================================================
# MinIO / Dify 環境變數
# =========================================================

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "pcb-dxf")
MINIO_SECURE = os.getenv("MINIO_SECURE", "true").lower() == "true"

DIFY_API_BASE = os.getenv("DIFY_API_BASE", "")
DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")

JOB_STORE: Dict[str, Dict[str, Any]] = {}


# =========================================================
# 共用工具
# =========================================================

def normalize_yes_no(value: str) -> str:
    v = str(value).strip().lower()
    if v in ["yes", "true", "1", "是", "y"]:
        return "Yes"
    if v in ["no", "false", "0", "否", "n"]:
        return "No"
    return "No"


def yes_no_to_bool(value: Any) -> bool:
    v = str(value).strip().lower()
    return v in ["yes", "true", "1", "是", "y"]


def safe_filename_name(filename: str) -> str:
    return (
        filename
        .replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
        .replace(":", "_")
        .replace("*", "_")
        .replace("?", "_")
        .replace('"', "_")
        .replace("<", "_")
        .replace(">", "_")
        .replace("|", "_")
    )


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def status_to_zh(status_code: str) -> str:
    mapping = {
        "recommended": "建議",
        "use_with_caution": "謹慎使用",
        "not_recommended": "不建議",
    }
    return mapping.get(status_code, status_code)


def risk_level_zh(risk_score: int) -> str:
    if risk_score >= 100:
        return "高風險"
    if risk_score >= 40:
        return "中風險"
    return "低風險"


def reasons_to_text(reasons: List[str]) -> str:
    if not reasons:
        return "符合目前尺寸與製程限制，無明顯重大風險。"
    return "；".join(reasons)


def make_public_api_download_url(object_key: str) -> str:
    return f"/api/pcb/download?object_key={object_key}"


def select_display_candidates(candidates: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    """
    候選方案排序邏輯。

    v0.9.9 修正重點：
    - 不再只使用固定候選清單，因此 20 x 14 mm 這類小板不會被限制在 2x3。
    - 若為小尺寸板，會優先採用程式計算出的 small_board_target，例如 10x12。
    - 一般尺寸板仍以可製造、pcs/panel、風險與長寬比綜合排序。
    """
    status_priority = {
        "recommended": 0,
        "use_with_caution": 1,
        "not_recommended": 2,
    }

    def sort_key(c):
        aspect_delta = abs(float(c.get("aspect_ratio", 99)) - PANEL_RULES["layout"]["preferred_aspect_ratio"])
        preferred_layout_penalty = int(c.get("preferred_layout_penalty", 50))
        over_dense_penalty = int(c.get("over_dense_penalty", 0))

        return (
            status_priority.get(c.get("status_code", ""), 9),
            preferred_layout_penalty,
            over_dense_penalty,
            -int(c.get("pcs_per_panel", 0)),
            int(c.get("risk_score", 9999)),
            aspect_delta,
            float(c.get("panel_length_mm", 9999)) * float(c.get("panel_width_mm", 9999)),
        )

    return sorted(candidates, key=sort_key)[:limit]


# =========================================================
# strategy_json 解析與程式規則策略
# =========================================================

def parse_strategy_json(strategy_json: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not strategy_json or not str(strategy_json).strip():
        return None, None

    raw = str(strategy_json).strip()
    raw = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    raw = raw.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed, None
        return None, "strategy_json parsed but is not an object"
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, None
            return None, "extracted strategy_json is not an object"
        except Exception as e:
            return None, f"json.loads extracted object failed: {str(e)}"

    return None, "strategy_json is not valid JSON"


def determine_base_split_method(
    has_bga_qfn: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
) -> str:
    if is_irregular_shape or has_bga_qfn or has_heavy_component:
        return "Router / Tab"
    return "V-cut"


def determine_rail_policy(
    split_method: str,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
    force_tooling_hole: bool = True,
) -> str:
    """
    判斷板邊需要兩側或四側。

    原則：
    1. 若需要 Tooling Hole / Fiducial / Router Tab，採四邊工藝邊。
    2. 若為異形板、BGA/QFN、DIP、重零件，採四邊工藝邊。
    3. 只有簡單矩形板且 V-cut、無治具需求時，才可允許兩側工藝邊。
    """
    if force_tooling_hole:
        return "four_sides"
    if "Router" in split_method or "Tab" in split_method:
        return "four_sides"
    if has_bga_qfn or has_dip or has_heavy_component or is_irregular_shape:
        return "four_sides"
    return "two_sides"


def sanitize_strategy(strategy: dict) -> dict:
    if not isinstance(strategy, dict):
        strategy = {}

    layout = strategy.get("layout") if isinstance(strategy.get("layout"), dict) else {}
    fid = strategy.get("fiducial_rule") if isinstance(strategy.get("fiducial_rule"), dict) else {}
    hole = strategy.get("tooling_hole_rule") if isinstance(strategy.get("tooling_hole_rule"), dict) else {}
    cutting = strategy.get("cutting_rule") if isinstance(strategy.get("cutting_rule"), dict) else {}

    method = cutting.get("method") or strategy.get("recommended_method") or "Router / Tab"
    if method not in ["V-cut", "V-CUT", "Router / Tab"]:
        method = "Router / Tab"

    if method == "V-CUT":
        method = "V-cut"

    default_gap = PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0

    def safe_float_from_dict(d: dict, key: str, default: float) -> float:
        try:
            return float(d.get(key, default))
        except Exception:
            return default

    def safe_int_from_dict(d: dict, key: str, default: int) -> int:
        try:
            return int(float(d.get(key, default)))
        except Exception:
            return default

    return {
        "strategy_source": strategy.get("strategy_source") or PANEL_RULES["source"],
        "recommended_method": method,
        "layout": {
            "columns": max(1, safe_int_from_dict(layout, "columns", 1)),
            "rows": max(1, safe_int_from_dict(layout, "rows", 1)),
            "gap_x_mm": max(PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0, safe_float_from_dict(layout, "gap_x_mm", default_gap)),
            "gap_y_mm": max(PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0, safe_float_from_dict(layout, "gap_y_mm", default_gap)),
            "rail_left_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], safe_float_from_dict(layout, "rail_left_mm", PANEL_RULES["rail"]["default_mm"])),
            "rail_right_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], safe_float_from_dict(layout, "rail_right_mm", PANEL_RULES["rail"]["default_mm"])),
            "rail_top_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], safe_float_from_dict(layout, "rail_top_mm", PANEL_RULES["rail"]["default_mm"])),
            "rail_bottom_mm": max(PANEL_RULES["rail"]["minimum_warning_mm"], safe_float_from_dict(layout, "rail_bottom_mm", PANEL_RULES["rail"]["default_mm"])),
        },
        "fiducial_rule": {
            "count": max(2, safe_int_from_dict(fid, "count", PANEL_RULES["fiducial"]["panel_count"])),
            "diameter_mm": safe_float_from_dict(fid, "diameter_mm", PANEL_RULES["fiducial"]["diameter_mm"]),
            "clearance_mm": max(PANEL_RULES["fiducial"]["clearance_mm"], safe_float_from_dict(fid, "clearance_mm", PANEL_RULES["fiducial"]["clearance_mm"])),
            "positions": fid.get("positions") if isinstance(fid.get("positions"), list) else PANEL_RULES["fiducial"]["positions"],
            "reason": fid.get("reason") or "依程式內建公司連板規範，Panel 光學點採三點對角防呆配置。",
        },
        "tooling_hole_rule": {
            "count": max(4, safe_int_from_dict(hole, "count", PANEL_RULES["tooling_hole"]["count"])),
            "diameter_mm": safe_float_from_dict(hole, "diameter_mm", PANEL_RULES["tooling_hole"]["diameter_mm"]),
            "clearance_mm": max(PANEL_RULES["tooling_hole"]["clearance_mm"], safe_float_from_dict(hole, "clearance_mm", PANEL_RULES["tooling_hole"]["clearance_mm"])),
            "positions": hole.get("positions") if isinstance(hole.get("positions"), list) else PANEL_RULES["tooling_hole"]["positions"],
            "reason": hole.get("reason") or "依程式內建公司連板規範，固定點採四點分布於對角四邊，並需由 ME/CAM 確認防呆距離。",
        },
        "cutting_rule": {
            "method": method,
            "reason": cutting.get("reason") or "依程式內建公司連板規範，異形板、BGA/QFN 或重零件優先採 Router / Tab。",
        },
        "risk_items": strategy.get("risk_items") if isinstance(strategy.get("risk_items"), list) else [],
        "me_cam_check_items": strategy.get("me_cam_check_items") if isinstance(strategy.get("me_cam_check_items"), list) else PANEL_RULES["check_items"],
    }


def build_program_rule_strategy(
    best_candidate: dict,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
    input_rail_width: float,
) -> dict:
    method = determine_base_split_method(
        has_bga_qfn=has_bga_qfn,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
    )

    gap = PANEL_RULES["cutting"]["router_min_gap_mm"] if method == "Router / Tab" else 0.0
    rail = max(input_rail_width, PANEL_RULES["rail"]["company_recommended_min_mm"])
    rail_policy = determine_rail_policy(method, has_bga_qfn, has_dip, has_heavy_component, is_irregular_shape, force_tooling_hole=True)

    risk_items = [
        "光學點及工具孔需距離板邊 5 mm 以上。",
        "單板之間距離不得超過 30 mm，避免 SMT Sensor 誤判。",
        "板邊、連接點、光學點與定位點需以白色線規劃。",
        "板邊需判斷兩側或四側；若需治具定位、Fiducial 或 Router Tab，採四邊工藝邊。",
        "連板圖面需標註單板尺寸、連板尺寸與連板數。",
        "MLCC 0805 以上若距板邊 5 mm 以內，應使用 Router 製程。",
        "金手指周圍不可設計連接點和 V-CUT 線。",
        "雙面零件排版需做讓位，零件側應朝外避免上件干涉。",
        "正式投產前需由 ME / CAM 工程師確認 V-CUT、Router、Fiducial、Tooling Hole、Tab 連接點與分板應力。",
    ]

    if method == "Router / Tab":
        risk_items.append("ROUTE 各切點間距至少 2 mm 以上，因銑刀直徑約 1.5 mm。")
        risk_items.append("Router / Tab 時，連接點不可同時作郵票孔和 V-CUT 線。")
    else:
        risk_items.append("V-CUT 線路徑上不得有干涉零件。")
        risk_items.append("零件與 V-CUT 線距離需大於 2 mm。")
        risk_items.append("V-CUT 線邊 5 mm 內零件高度不可超過 10 mm。")
        risk_items.append("V-CUT 深度需為 PCB 兩邊各 1/3，開槽角度需 30 度。")

    if has_bga_qfn:
        risk_items.append("有 BGA/QFN，需確認元件距分板邊界與 Fiducial 的安全距離，避免分板應力造成焊點裂紋。")

    if has_dip:
        risk_items.append("有 DIP，需確認波峰焊方向、錫流方向與治具需求。")

    if has_heavy_component:
        risk_items.append("有重零件，需評估過爐板彎、支撐治具與分板應力。")

    if is_irregular_shape:
        risk_items.append("異形板不建議直接使用 V-CUT，建議採 Router / Tab。")

    strategy = {
        "strategy_source": PANEL_RULES["source"],
        "recommended_method": method,
        "rail_policy": rail_policy,
        "layout": {
            "columns": int(best_candidate.get("columns", best_candidate.get("x_count", 1))),
            "rows": int(best_candidate.get("rows", best_candidate.get("y_count", 1))),
            "gap_x_mm": gap,
            "gap_y_mm": gap,
            "rail_left_mm": rail,
            "rail_right_mm": rail,
            "rail_top_mm": rail if rail_policy == "four_sides" else 0.0,
            "rail_bottom_mm": rail if rail_policy == "four_sides" else 0.0,
        },
        "fiducial_rule": {
            "count": PANEL_RULES["fiducial"]["panel_count"],
            "diameter_mm": PANEL_RULES["fiducial"]["diameter_mm"],
            "clearance_mm": PANEL_RULES["fiducial"]["clearance_mm"],
            "positions": PANEL_RULES["fiducial"]["positions"],
            "reason": "依程式內建公司連板規範，Panel 光學點採 3 點分布於對角並具防呆設計。",
        },
        "tooling_hole_rule": {
            "count": PANEL_RULES["tooling_hole"]["count"],
            "diameter_mm": PANEL_RULES["tooling_hole"]["diameter_mm"],
            "clearance_mm": PANEL_RULES["tooling_hole"]["clearance_mm"],
            "positions": PANEL_RULES["tooling_hole"]["positions"],
            "reason": "依程式內建公司連板規範，固定點採 4 點分布於對角四邊，並由 ME/CAM 確認防呆距離。",
        },
        "cutting_rule": {
            "method": method,
            "reason": "依程式內建公司連板規範，異形板、BGA/QFN 或重零件優先採 Router / Tab；一般矩形板且無高風險元件時才可評估 V-CUT。",
        },
        "risk_items": risk_items,
        "me_cam_check_items": PANEL_RULES["check_items"],
    }

    return sanitize_strategy(strategy)


def get_strategy_for_backend(
    strategy_json: str,
    best_candidate: dict,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
    input_rail_width: float,
) -> Tuple[dict, bool, Optional[str]]:
    parsed_strategy, parse_error = parse_strategy_json(strategy_json)

    if parsed_strategy:
        strategy = sanitize_strategy(parsed_strategy)
        if not strategy.get("strategy_source"):
            strategy["strategy_source"] = "dify_strategy_json"
        return strategy, True, None

    program_strategy = build_program_rule_strategy(
        best_candidate=best_candidate,
        has_bga_qfn=has_bga_qfn,
        has_dip=has_dip,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
        input_rail_width=input_rail_width,
    )

    return program_strategy, True, parse_error


def apply_strategy_to_candidate(
    base_candidate: Dict[str, Any],
    strategy: Optional[Dict[str, Any]],
    board_w: float,
    board_h: float,
    default_rail_width: float,
) -> Dict[str, Any]:
    candidate = dict(base_candidate)

    if not strategy:
        return candidate

    layout = strategy.get("layout", {})
    cutting_rule = strategy.get("cutting_rule", {})

    columns = max(1, to_int(layout.get("columns", candidate.get("columns", 1)), 1))
    rows = max(1, to_int(layout.get("rows", candidate.get("rows", 1)), 1))
    gap_x = max(0.0, to_float(layout.get("gap_x_mm", candidate.get("gap_x_mm", 0.0)), 0.0))
    gap_y = max(0.0, to_float(layout.get("gap_y_mm", candidate.get("gap_y_mm", 0.0)), 0.0))

    rail_left = max(0.0, to_float(layout.get("rail_left_mm", default_rail_width), default_rail_width))
    rail_right = max(0.0, to_float(layout.get("rail_right_mm", default_rail_width), default_rail_width))
    rail_top = max(0.0, to_float(layout.get("rail_top_mm", default_rail_width), default_rail_width))
    rail_bottom = max(0.0, to_float(layout.get("rail_bottom_mm", default_rail_width), default_rail_width))

    split_method = cutting_rule.get("method") or strategy.get("recommended_method") or candidate.get("split_method", "V-cut")

    # ROUTE 各連板間距至少 2 mm 以上。
    if "Router" in str(split_method) or "Tab" in str(split_method):
        min_route_gap = PANEL_RULES["cutting"]["router_min_gap_mm"]
        gap_x = max(gap_x, min_route_gap)
        gap_y = max(gap_y, min_route_gap)

    panel_w = rail_left + columns * board_w + max(columns - 1, 0) * gap_x + rail_right
    panel_h = rail_bottom + rows * board_h + max(rows - 1, 0) * gap_y + rail_top

    aspect_ratio = max(panel_w, panel_h) / max(min(panel_w, panel_h), 0.001)

    candidate.update({
        "panel_type": f"{columns}x{rows}",
        "x_count": columns,
        "y_count": rows,
        "columns": columns,
        "rows": rows,
        "gap_x_mm": gap_x,
        "gap_y_mm": gap_y,
        "rail_left_mm": rail_left,
        "rail_right_mm": rail_right,
        "rail_top_mm": rail_top,
        "rail_bottom_mm": rail_bottom,
        "panel_length_mm": round(panel_w, 2),
        "panel_width_mm": round(panel_h, 2),
        "panel_size": f"{panel_w:.1f} x {panel_h:.1f} mm",
        "pcs_per_panel": columns * rows,
        "aspect_ratio": round(aspect_ratio, 2),
        "split_method": split_method,
        "rail_policy": strategy.get("rail_policy", "four_sides"),
        "strategy_override_applied": True,
        "strategy_source": strategy.get("strategy_source", PANEL_RULES["source"]),
        "rule_source": PANEL_RULES["source"],
    })

    return candidate


def update_result_with_strategy_candidate(result: Dict[str, Any], strategy_candidate: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(result)
    all_candidates = list(result.get("all_candidates", []))

    updated["best_candidate"] = strategy_candidate

    filtered = [
        c for c in all_candidates
        if c.get("panel_type") != strategy_candidate.get("panel_type")
    ]

    updated["display_candidates"] = [strategy_candidate] + select_display_candidates(filtered, limit=2)
    updated["all_candidates"] = all_candidates
    updated["candidates"] = all_candidates
    return updated


# =========================================================
# MinIO 工具
# =========================================================

def get_minio_client() -> Minio:
    if not MINIO_ENDPOINT:
        raise HTTPException(status_code=500, detail="MINIO_ENDPOINT is not configured")
    if not MINIO_ACCESS_KEY:
        raise HTTPException(status_code=500, detail="MINIO_ACCESS_KEY is not configured")
    if not MINIO_SECRET_KEY:
        raise HTTPException(status_code=500, detail="MINIO_SECRET_KEY is not configured")

    endpoint = MINIO_ENDPOINT.replace("https://", "").replace("http://", "")

    return Minio(
        endpoint=endpoint,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def ensure_bucket_exists():
    client = get_minio_client()
    try:
        found = client.bucket_exists(MINIO_BUCKET)
        if not found:
            client.make_bucket(MINIO_BUCKET)
    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO bucket error: {str(e)}")


def upload_file_to_minio(local_path: str, object_name: str, content_type: str = "application/dxf"):
    ensure_bucket_exists()
    client = get_minio_client()

    try:
        client.fput_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            file_path=local_path,
            content_type=content_type,
        )
    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO upload error: {str(e)}")


def download_file_from_minio(object_name: str, local_path: str):
    ensure_bucket_exists()
    client = get_minio_client()

    try:
        client.fget_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            file_path=local_path,
        )
    except S3Error as e:
        raise HTTPException(status_code=404, detail=f"MinIO object not found or download failed: {str(e)}")


def get_presigned_download_url(object_name: str, hours: int = 24) -> str:
    ensure_bucket_exists()
    client = get_minio_client()

    try:
        return client.presigned_get_object(
            bucket_name=MINIO_BUCKET,
            object_name=object_name,
            expires=timedelta(hours=hours),
        )
    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"MinIO presigned url error: {str(e)}")


# =========================================================
# 基礎 API
# =========================================================

@app.get("/", response_class=HTMLResponse)
def root():
    """
    首頁保留目前 index.html 畫面。
    若 index.html 不存在，回傳健康狀態 JSON。
    """
    html_path = os.path.join(os.getcwd(), "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    return HTMLResponse(
        json.dumps({
            "status": "ok",
            "service": "PCB Panelization API",
            "version": "0.9.10-real-dxf-index-compatible",
            "program_rules_applied_by_default": True,
            "rule_version": PANEL_RULES["version"],
            "message": "index.html not found, but API is running.",
        }, ensure_ascii=False),
        media_type="application/json",
    )


@app.get("/api/panel-rules")
def get_panel_rules():
    return PANEL_RULES


@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    html_path = os.path.join(os.getcwd(), "upload.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="upload.html not found")

    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/health/minio")
def health_minio():
    try:
        client = get_minio_client()
        bucket_exists = client.bucket_exists(MINIO_BUCKET)
        if not bucket_exists:
            client.make_bucket(MINIO_BUCKET)

        return {
            "status": "ok",
            "message": "MinIO connection success",
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "secure": MINIO_SECURE,
            "bucket_created_or_exists": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "message": "MinIO connection failed",
            "endpoint": MINIO_ENDPOINT,
            "secure": MINIO_SECURE,
            "bucket": MINIO_BUCKET,
            "error_type": type(e).__name__,
            "error_detail": str(e),
        }


@app.get("/api/health/dify")
def health_dify():
    return {
        "status": "ok" if DIFY_API_BASE and DIFY_API_KEY else "error",
        "DIFY_API_BASE_configured": bool(DIFY_API_BASE),
        "DIFY_API_KEY_configured": bool(DIFY_API_KEY),
        "DIFY_API_BASE": DIFY_API_BASE,
        "expected_workflow_run_url": f"{DIFY_API_BASE.rstrip('/')}/workflows/run" if DIFY_API_BASE else "",
    }


# =========================================================
# 候選方案
# =========================================================

def build_dynamic_candidate_patterns(
    single_board_length: float,
    single_board_width: float,
    rail_left: float,
    rail_right: float,
    rail_top: float,
    rail_bottom: float,
    gap: float,
    smt_max_length: float,
    smt_max_width: float,
    ict_max_length: float,
    ict_max_width: float,
) -> List[Tuple[int, int]]:
    """
    依設備尺寸自動產生候選連板數。

    修正原因：
    原本只有固定候選，例如 1x1、2x2、2x3、3x2，導致 20 x 14 mm 小板只會選到 2x3。
    新版會依 SMT / ICT / 公司最大尺寸自動計算可行欄列數，因此小板可產生 10x12 這類高效率連板。
    """
    max_length_limit = min(
        PANEL_RULES["panel_size"]["absolute_max_length_mm"],
        smt_max_length if smt_max_length > 0 else PANEL_RULES["panel_size"]["absolute_max_length_mm"],
        ict_max_length if ict_max_length > 0 else PANEL_RULES["panel_size"]["absolute_max_length_mm"],
    )
    max_width_limit = min(
        PANEL_RULES["panel_size"]["absolute_max_width_mm"],
        smt_max_width if smt_max_width > 0 else PANEL_RULES["panel_size"]["absolute_max_width_mm"],
        ict_max_width if ict_max_width > 0 else PANEL_RULES["panel_size"]["absolute_max_width_mm"],
    )

    def max_count(limit: float, rail_a: float, rail_b: float, board: float, route_gap: float) -> int:
        if board <= 0:
            return 1
        # rail_a + rail_b + n*board + (n-1)*gap <= limit
        # n*(board+gap) <= limit - rail_a - rail_b + gap
        value = int((limit - rail_a - rail_b + route_gap) // (board + route_gap))
        return max(1, min(value, 30))

    max_x = max_count(max_length_limit, rail_left, rail_right, single_board_length, gap)
    max_y = max_count(max_width_limit, rail_bottom, rail_top, single_board_width, gap)

    patterns = set(PANEL_RULES["layout"].get("candidate_patterns", []))

    for x in range(1, max_x + 1):
        for y in range(1, max_y + 1):
            patterns.add((x, y))

    # 小尺寸板常用高效率候選，確保 20 x 14 mm 類小板會被納入評估。
    if single_board_length <= 30.0 and single_board_width <= 20.0:
        for p in [(8, 10), (10, 10), (10, 12), (12, 10), (12, 12), (14, 10), (10, 14)]:
            patterns.add(p)

    return sorted(patterns, key=lambda p: (p[0] * p[1], p[0], p[1]))


def get_preferred_layout_penalty(
    x_count: int,
    y_count: int,
    single_board_length: float,
    single_board_width: float,
    panel_length: float,
    panel_width: float,
    pcs_per_panel: int,
) -> Tuple[int, int, str]:
    """
    產生排序用 penalty。

    對 20 x 14 mm 這類小板，依使用者需求：
    - 不應再只做 2x3。
    - 優先建議 10x12。

    這裡不是硬寫所有產品都 10x12，而是只有小板類產品才把 10x12 作為最佳模板目標。
    """
    is_small_board = single_board_length <= 30.0 and single_board_width <= 20.0

    if is_small_board:
        target_x = 10
        target_y = 12
        layout_distance = abs(x_count - target_x) * 10 + abs(y_count - target_y) * 10

        # 避免比 10x12 過度密集的 12x12 / 14x14 自動勝出。
        over_dense_penalty = max(0, pcs_per_panel - (target_x * target_y))
        note = "小尺寸板採高效率連板邏輯，優先接近 10x12，而非固定 2x3。"
        return layout_distance, over_dense_penalty, note

    # 一般板：不指定固定版數，仍以 pcs/panel 與長寬比評估。
    target_aspect = PANEL_RULES["layout"].get("preferred_aspect_ratio", 1.5)
    aspect = max(panel_length, panel_width) / max(min(panel_length, panel_width), 0.001)
    aspect_penalty = int(abs(aspect - target_aspect) * 10)
    return 50 + aspect_penalty, 0, "一般尺寸板依 pcs/panel、風險分數與長寬比綜合排序。"


def calculate_candidates(
    single_board_length: float,
    single_board_width: float,
    rail_width: float,
    smt_max_length: float,
    smt_max_width: float,
    ict_max_length: float,
    ict_max_width: float,
    has_bga_qfn: bool,
    has_dip: bool,
    has_heavy_component: bool,
    is_irregular_shape: bool,
) -> dict:

    candidates = []

    split_method = determine_base_split_method(
        has_bga_qfn=has_bga_qfn,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
    )

    rail_policy = determine_rail_policy(
        split_method=split_method,
        has_bga_qfn=has_bga_qfn,
        has_dip=has_dip,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
        force_tooling_hole=True,
    )

    gap = PANEL_RULES["cutting"]["router_min_gap_mm"] if split_method == "Router / Tab" else 0.0
    rail = max(rail_width, PANEL_RULES["rail"]["company_recommended_min_mm"])

    rail_left = rail
    rail_right = rail
    rail_top = rail if rail_policy == "four_sides" else 0.0
    rail_bottom = rail if rail_policy == "four_sides" else 0.0

    candidate_patterns = build_dynamic_candidate_patterns(
        single_board_length=single_board_length,
        single_board_width=single_board_width,
        rail_left=rail_left,
        rail_right=rail_right,
        rail_top=rail_top,
        rail_bottom=rail_bottom,
        gap=gap,
        smt_max_length=smt_max_length,
        smt_max_width=smt_max_width,
        ict_max_length=ict_max_length,
        ict_max_width=ict_max_width,
    )

    for x_count, y_count in candidate_patterns:
        panel_length = rail_left + single_board_length * x_count + gap * max(x_count - 1, 0) + rail_right
        panel_width = rail_bottom + single_board_width * y_count + gap * max(y_count - 1, 0) + rail_top

        pcs_per_panel = x_count * y_count
        aspect_ratio = max(panel_length, panel_width) / max(min(panel_length, panel_width), 0.001)

        reasons = []
        risk_score = 0

        if panel_length > PANEL_RULES["panel_size"]["absolute_max_length_mm"] or panel_width > PANEL_RULES["panel_size"]["absolute_max_width_mm"]:
            reasons.append("Panel 尺寸超過公司最大連板尺寸 510 x 360 mm，不可推薦。")
            risk_score += PANEL_RULES["risk_score"]["exceed_absolute_max"]

        if panel_length < PANEL_RULES["panel_size"]["juki_min_length_mm"] or panel_width < PANEL_RULES["panel_size"]["juki_min_width_mm"]:
            reasons.append("Panel 尺寸低於 JUKI / SMT 最小 50 x 50 mm，可能無法穩定輸送。")
            risk_score += PANEL_RULES["risk_score"]["below_min_size"]

        if panel_length > smt_max_length:
            reasons.append(f"Panel 長度 {panel_length:.1f} mm 超過 SMT 最大長度 {smt_max_length:.1f} mm。")
            risk_score += PANEL_RULES["risk_score"]["exceed_smt_input"]

        if panel_width > smt_max_width:
            reasons.append(f"Panel 寬度 {panel_width:.1f} mm 超過 SMT 最大寬度 {smt_max_width:.1f} mm。")
            risk_score += PANEL_RULES["risk_score"]["exceed_smt_input"]

        if ict_max_length > 0 and ict_max_width > 0:
            if panel_length > ict_max_length or panel_width > ict_max_width:
                reasons.append(
                    f"Panel 尺寸 {panel_length:.1f} x {panel_width:.1f} mm 可能超過 ICT 治具限制 {ict_max_length:.1f} x {ict_max_width:.1f} mm。"
                )
                risk_score += PANEL_RULES["risk_score"]["exceed_ict_input"]

        if PANEL_RULES["layout"]["near_square_min"] <= aspect_ratio <= PANEL_RULES["layout"]["near_square_max"]:
            reasons.append("Panel 長寬比趨近正方形，依公司規範需謹慎評估板彎，建議接近 3:2。")
            risk_score += PANEL_RULES["risk_score"]["near_square"]

        if aspect_ratio > PANEL_RULES["layout"]["not_recommended_aspect_ratio"]:
            reasons.append(f"Panel 長寬比 {aspect_ratio:.2f} 大於 4.0，板彎或輸送不穩風險高。")
            risk_score += PANEL_RULES["risk_score"]["aspect_gt_4"]
        elif aspect_ratio > PANEL_RULES["layout"]["caution_aspect_ratio"]:
            reasons.append(f"Panel 長寬比 {aspect_ratio:.2f} 大於 3.0，可能有板彎或輸送不穩風險。")
            risk_score += PANEL_RULES["risk_score"]["aspect_gt_3"]

        if is_irregular_shape:
            reasons.append("異形板不建議直接使用 V-CUT，建議 Router / Tab。")
            risk_score += PANEL_RULES["risk_score"]["irregular_shape"]

        if has_bga_qfn:
            reasons.append("有 BGA/QFN，需確認距 V-CUT 或 Router 邊界安全距離，避免分板應力。")
            risk_score += PANEL_RULES["risk_score"]["bga_qfn"]

        if has_dip:
            reasons.append("有 DIP，需確認波峰焊方向、錫流方向與治具需求。")
            risk_score += PANEL_RULES["risk_score"]["dip"]

        if has_heavy_component:
            reasons.append("有重零件，需評估過爐板彎、支撐治具與分板應力。")
            risk_score += PANEL_RULES["risk_score"]["heavy_component"]

        if rail_width < PANEL_RULES["rail"]["company_recommended_min_mm"]:
            reasons.append(f"輸入工藝邊 {rail_width:.1f} mm 低於公司建議 8~10 mm，本系統已以 {rail:.1f} mm 進行保守設計。")

        preferred_layout_penalty, over_dense_penalty, dynamic_note = get_preferred_layout_penalty(
            x_count=x_count,
            y_count=y_count,
            single_board_length=single_board_length,
            single_board_width=single_board_width,
            panel_length=panel_length,
            panel_width=panel_width,
            pcs_per_panel=pcs_per_panel,
        )

        reasons.append(f"板邊策略：{rail_policy}；若需 Tooling Hole / Fiducial / Router Tab，採四邊工藝邊。")
        reasons.append(dynamic_note)

        if risk_score >= 100:
            status_code = "not_recommended"
        elif risk_score >= 40:
            status_code = "use_with_caution"
        else:
            status_code = "recommended"

        candidates.append({
            "panel_type": f"{x_count}x{y_count}",
            "x_count": x_count,
            "y_count": y_count,
            "columns": x_count,
            "rows": y_count,
            "gap_x_mm": gap,
            "gap_y_mm": gap,
            "rail_left_mm": rail_left,
            "rail_right_mm": rail_right,
            "rail_top_mm": rail_top,
            "rail_bottom_mm": rail_bottom,
            "rail_policy": rail_policy,
            "panel_length_mm": round(panel_length, 2),
            "panel_width_mm": round(panel_width, 2),
            "panel_size": f"{panel_length:.1f} x {panel_width:.1f} mm",
            "pcs_per_panel": pcs_per_panel,
            "aspect_ratio": round(aspect_ratio, 2),
            "split_method": split_method,
            "risk_score": int(risk_score),
            "risk_level_zh": risk_level_zh(int(risk_score)),
            "status_code": status_code,
            "status": status_to_zh(status_code),
            "status_zh": status_to_zh(status_code),
            "reasons": reasons,
            "reason_text": reasons_to_text(reasons),
            "preferred_layout_penalty": preferred_layout_penalty,
            "over_dense_penalty": over_dense_penalty,
            "dynamic_candidate_generation": True,
            "rule_source": PANEL_RULES["source"],
        })

    display_candidates = select_display_candidates(candidates, limit=3)

    recommended_candidates = [c for c in candidates if c["status_code"] == "recommended"]
    caution_candidates = [c for c in candidates if c["status_code"] == "use_with_caution"]
    not_recommended_candidates = [c for c in candidates if c["status_code"] == "not_recommended"]

    if recommended_candidates:
        best_candidate = select_display_candidates(recommended_candidates, limit=1)[0]
    elif caution_candidates:
        best_candidate = select_display_candidates(caution_candidates, limit=1)[0]
    else:
        best_candidate = select_display_candidates(not_recommended_candidates, limit=1)[0]

    return {
        "best_candidate": best_candidate,
        "display_candidates": display_candidates,
        "candidates": candidates,
        "all_candidates": candidates,
        "recommended_candidates": recommended_candidates,
        "caution_candidates": caution_candidates,
        "not_recommended_candidates": not_recommended_candidates,
        "rule_source": PANEL_RULES["source"],
        "rule_version": PANEL_RULES["version"],
        "dynamic_candidate_generation": True,
    }


def build_comparison_table_markdown(candidates: List[Dict[str, Any]], limit: int = 3) -> str:
    display_candidates = select_display_candidates(candidates, limit=limit)

    lines = []
    lines.append("| 方案 | Panel 尺寸 | pcs/panel | 分板方式 | 板邊策略 | 風險分數 | 風險等級 | 狀態 | 建議原因 |")
    lines.append("|---|---:|---:|---|---|---:|---|---|---|")

    for c in display_candidates:
        lines.append(
            f"| {c['panel_type']} "
            f"| {c['panel_size']} "
            f"| {c['pcs_per_panel']} "
            f"| {c['split_method']} "
            f"| {c.get('rail_policy', 'four_sides')} "
            f"| {c['risk_score']} "
            f"| {c['risk_level_zh']} "
            f"| {c['status_zh']} "
            f"| {c['reason_text']} |"
        )

    return "\n".join(lines)


def build_caution_and_not_summary(candidates: List[Dict[str, Any]], limit: int = 3) -> str:
    display_candidates = select_display_candidates(candidates, limit=limit)

    items = [
        c for c in display_candidates
        if c["status_code"] in ["use_with_caution", "not_recommended"]
    ]

    if not items:
        return "本次顯示的前三個候選方案皆為建議方案，未出現謹慎使用或不建議方案。"

    return "\n".join([f"- 方案 {c['panel_type']}：{c['status_zh']}，原因：{c['reason_text']}" for c in items])


# =========================================================
# DXF 幾何工具
# =========================================================

def get_modelspace_bbox(doc) -> Optional[Tuple[float, float, float, float]]:
    try:
        msp = doc.modelspace()
        ext = bbox.extents(msp, fast=True)

        if not ext.has_data:
            return None

        min_x = float(ext.extmin.x)
        min_y = float(ext.extmin.y)
        max_x = float(ext.extmax.x)
        max_y = float(ext.extmax.y)

        if max_x <= min_x or max_y <= min_y:
            return None

        return min_x, min_y, max_x, max_y

    except Exception:
        return None


def ensure_layer(doc, name: str, color: int = 7):
    try:
        if name not in doc.layers:
            doc.layers.add(name=name, color=color)
        else:
            doc.layers.get(name).dxf.color = color
    except Exception:
        pass


def transform_entity_safe(entity, matrix: Matrix44) -> bool:
    try:
        entity.transform(matrix)
        return True
    except Exception:
        return False


def add_lwpolyline_rect(msp, x: float, y: float, w: float, h: float, layer: str):
    msp.add_lwpolyline(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)],
        dxfattribs={"layer": layer, "closed": True},
    )


def add_line(msp, x1: float, y1: float, x2: float, y2: float, layer: str):
    msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})


def add_circle(msp, x: float, y: float, r: float, layer: str):
    msp.add_circle(center=(x, y), radius=r, dxfattribs={"layer": layer})


def add_text(msp, text: str, x: float, y: float, height: float, layer: str):
    try:
        msp.add_text(
            text,
            dxfattribs={
                "layer": layer,
                "height": height,
                "insert": (x, y),
            },
        )
    except Exception:
        pass


def add_leader_callout(
    msp,
    start_x: float,
    start_y: float,
    elbow_x: float,
    elbow_y: float,
    text_x: float,
    text_y: float,
    text: str,
    layer: str = "PANEL_DIMENSION",
    text_height: float = 1.8,
):
    """以白色線建立引線標註：起點 -> 轉折點 -> 文字底線。"""
    add_line(msp, start_x, start_y, elbow_x, elbow_y, layer)
    add_line(msp, elbow_x, elbow_y, text_x - 1.0, text_y, layer)
    add_line(msp, text_x - 1.0, text_y, text_x + max(35.0, len(text) * text_height * 1.7), text_y, layer)
    add_text(msp, text, text_x, text_y + 0.8, text_height, layer)


def add_dimension_line(
    msp,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    text: str,
    text_x: float,
    text_y: float,
    layer: str = "PANEL_DIMENSION",
    text_height: float = 1.8,
    extension: float = 3.0,
):
    """簡易尺寸線，含兩端輔助線與數值。"""
    add_line(msp, x1, y1, x2, y2, layer)
    if abs(y2 - y1) < abs(x2 - x1):
        add_line(msp, x1, y1 - extension, x1, y1 + extension, layer)
        add_line(msp, x2, y2 - extension, x2, y2 + extension, layer)
    else:
        add_line(msp, x1 - extension, y1, x1 + extension, y1, layer)
        add_line(msp, x2 - extension, y2, x2 + extension, y2, layer)
    add_text(msp, text, text_x, text_y, text_height, layer)


def position_to_xy(position: str, panel_w: float, panel_h: float, margin: float) -> Tuple[float, float]:
    p = str(position).strip().lower()

    mapping = {
        "bottom-left": (margin, margin),
        "bottom-right": (panel_w - margin, margin),
        "top-left": (margin, panel_h - margin),
        "top-right": (panel_w - margin, panel_h - margin),
        "left-top": (margin, panel_h - margin),
        "right-top": (panel_w - margin, panel_h - margin),
        "left-bottom": (margin, margin),
        "right-bottom": (panel_w - margin, margin),
        "center-left": (margin, panel_h / 2.0),
        "center-right": (panel_w - margin, panel_h / 2.0),
        "top-center": (panel_w / 2.0, panel_h - margin),
        "bottom-center": (panel_w / 2.0, margin),
        "center": (panel_w / 2.0, panel_h / 2.0),
    }

    return mapping.get(p, (margin, margin))


def normalize_positions(positions: Any, default_positions: List[str], count: int) -> List[str]:
    if isinstance(positions, list):
        output = [str(p) for p in positions if str(p).strip()]
    else:
        output = []

    if not output:
        output = list(default_positions)

    while len(output) < count:
        for p in default_positions:
            if len(output) >= count:
                break
            output.append(p)

    return output[:count]


def auto_determine_panel_features(
    panel_w: float,
    panel_h: float,
    rail_width: float,
    columns: int,
    rows: int,
    split_method: str,
    has_bga_qfn: bool = False,
    has_dip: bool = False,
    has_heavy_component: bool = False,
    is_irregular_shape: bool = False,
    strategy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    warnings: List[str] = []
    notes: List[str] = []

    fiducial_rule = {}
    tooling_hole_rule = {}

    if strategy and isinstance(strategy.get("fiducial_rule"), dict):
        fiducial_rule = strategy.get("fiducial_rule", {})

    if strategy and isinstance(strategy.get("tooling_hole_rule"), dict):
        tooling_hole_rule = strategy.get("tooling_hole_rule", {})

    fiducial_diameter_mm = to_float(fiducial_rule.get("diameter_mm", PANEL_RULES["fiducial"]["diameter_mm"]), PANEL_RULES["fiducial"]["diameter_mm"])
    fiducial_radius_mm = fiducial_diameter_mm / 2.0
    fiducial_count = to_int(fiducial_rule.get("count", PANEL_RULES["fiducial"]["panel_count"]), PANEL_RULES["fiducial"]["panel_count"])
    fiducial_clearance_mm = max(PANEL_RULES["fiducial"]["clearance_mm"], to_float(fiducial_rule.get("clearance_mm", PANEL_RULES["fiducial"]["clearance_mm"]), PANEL_RULES["fiducial"]["clearance_mm"]))

    tooling_hole_diameter_mm = to_float(tooling_hole_rule.get("diameter_mm", PANEL_RULES["tooling_hole"]["diameter_mm"]), PANEL_RULES["tooling_hole"]["diameter_mm"])
    tooling_hole_radius_mm = tooling_hole_diameter_mm / 2.0
    tooling_hole_count = to_int(tooling_hole_rule.get("count", PANEL_RULES["tooling_hole"]["count"]), PANEL_RULES["tooling_hole"]["count"])
    tooling_clearance_mm = max(PANEL_RULES["tooling_hole"]["clearance_mm"], to_float(tooling_hole_rule.get("clearance_mm", PANEL_RULES["tooling_hole"]["clearance_mm"]), PANEL_RULES["tooling_hole"]["clearance_mm"]))

    fiducial_count = max(3, fiducial_count)
    tooling_hole_count = max(4, tooling_hole_count)

    tooling_margin = max(tooling_clearance_mm, rail_width / 2.0)
    fiducial_margin = max(fiducial_clearance_mm, rail_width / 2.0)

    tooling_margin = min(tooling_margin, max(panel_w / 4.0, 1.0), max(panel_h / 4.0, 1.0))
    fiducial_margin = min(fiducial_margin, max(panel_w / 4.0, 1.0), max(panel_h / 4.0, 1.0))

    if rail_width < PANEL_RULES["rail"]["company_recommended_min_mm"]:
        warnings.append(f"工藝邊 {rail_width:.1f} mm 低於公司建議 8~10 mm，請 ME/CAM 確認是否需加寬。")

    if rail_width < fiducial_clearance_mm:
        warnings.append(f"工藝邊 {rail_width:.1f} mm 小於 Fiducial clearance {fiducial_clearance_mm:.1f} mm，需確認光學點距板邊。")

    if rail_width < tooling_clearance_mm:
        warnings.append(f"工藝邊 {rail_width:.1f} mm 小於 Tooling Hole clearance {tooling_clearance_mm:.1f} mm，需確認工具孔距板邊。")

    if has_bga_qfn:
        notes.append("有 BGA/QFN，需確認 Fiducial 是否足以支援高精度貼裝，並確認分板邊界安全距離。")

    if has_dip:
        notes.append("有 DIP，Tooling Hole 與治具定位需確認不干涉波峰焊治具與錫流方向。")

    if has_heavy_component:
        notes.append("有重零件，需確認定位孔與支撐治具是否足以降低過爐板彎。")

    if is_irregular_shape or "Router" in split_method or "Tab" in split_method:
        notes.append("異形板或 Router / Tab 分板時，Fiducial 與 Tooling Hole 優先放在 Panel 工藝邊。")

    tooling_positions = normalize_positions(tooling_hole_rule.get("positions"), PANEL_RULES["tooling_hole"]["positions"], tooling_hole_count)
    fiducial_positions = normalize_positions(fiducial_rule.get("positions"), PANEL_RULES["fiducial"]["positions"], fiducial_count)

    tooling_holes = []
    for index, position in enumerate(tooling_positions):
        x, y = position_to_xy(position, panel_w, panel_h, tooling_margin)
        tooling_holes.append({
            "name": f"TH{index + 1}",
            "x": round(x, 3),
            "y": round(y, 3),
            "diameter_mm": tooling_hole_diameter_mm,
            "radius_mm": tooling_hole_radius_mm,
            "location": position,
        })

    fiducials = []
    for index, position in enumerate(fiducial_positions):
        x, y = position_to_xy(position, panel_w, panel_h, fiducial_margin)
        fiducials.append({
            "name": f"FD{index + 1}",
            "x": round(x, 3),
            "y": round(y, 3),
            "diameter_mm": fiducial_diameter_mm,
            "radius_mm": fiducial_radius_mm,
            "location": position,
        })

    rule_summary = (
        f"依程式內建公司連板規範產生：Panel 光學點 {len(fiducials)} 點，直徑 {fiducial_diameter_mm:.2f} mm；"
        f"Tooling Hole {len(tooling_holes)} 點，直徑 {tooling_hole_diameter_mm:.2f} mm；"
        f"距板邊 clearance 不低於 5.0 mm，並包含固定點防呆提醒。"
    )

    return {
        "rule_version": "company_panel_feature_rule_v1.4_me_template_detail_edge_routertab",
        "strategy_used": bool(strategy),
        "rule_source": PANEL_RULES["source"],
        "fiducials": fiducials,
        "tooling_holes": tooling_holes,
        "fiducial_count": len(fiducials),
        "tooling_hole_count": len(tooling_holes),
        "fiducial_diameter_mm": fiducial_diameter_mm,
        "tooling_hole_diameter_mm": tooling_hole_diameter_mm,
        "fiducial_rule_from_strategy": fiducial_rule,
        "tooling_hole_rule_from_strategy": tooling_hole_rule,
        "warnings": warnings,
        "notes": notes,
        "rule_summary": rule_summary,
    }


def draw_panel_features(
    msp,
    feature_result: Dict[str, Any],
    tooling_layer: str = "PANEL_TOOLING",
    fiducial_layer: str = "PANEL_FIDUCIAL",
    text_layer: str = "PANEL_TEXT",
):
    for hole in feature_result.get("tooling_holes", []):
        x = float(hole["x"])
        y = float(hole["y"])
        r = float(hole["radius_mm"])
        name = str(hole["name"])
        add_circle(msp, x, y, r, tooling_layer)
        add_text(msp, name, x + r + 0.8, y + r + 0.8, 1.5, text_layer)

    for fid in feature_result.get("fiducials", []):
        x = float(fid["x"])
        y = float(fid["y"])
        r = float(fid["radius_mm"])
        name = str(fid["name"])
        add_circle(msp, x, y, r, fiducial_layer)
        add_text(msp, name, x + r + 0.8, y + r + 0.8, 1.5, text_layer)


def draw_edge_tabs(
    msp,
    panel_x0: float,
    panel_y0: float,
    rail_left: float,
    rail_bottom: float,
    board_w: float,
    board_h: float,
    pitch_x: float,
    pitch_y: float,
    columns: int,
    rows: int,
    tab_length: float,
    tab_width: float,
    layer: str = "PANEL_TAB",
):
    """
    在板邊與工藝邊交界處規劃 Tab 點。
    以白色線畫矩形 Tab，供 ME/CAM 檢查實際分板應力與切點間距。
    """
    for row in range(rows):
        for col in range(columns):
            bx = panel_x0 + rail_left + col * pitch_x
            by = panel_y0 + rail_bottom + row * pitch_y

            # 左側板邊 tab
            for ratio in [0.30, 0.70]:
                y = by + board_h * ratio
                add_lwpolyline_rect(msp, bx - tab_width / 2.0, y - tab_length / 2.0, tab_width, tab_length, layer)

            # 右側板邊 tab
            for ratio in [0.30, 0.70]:
                y = by + board_h * ratio
                add_lwpolyline_rect(msp, bx + board_w - tab_width / 2.0, y - tab_length / 2.0, tab_width, tab_length, layer)

            # 下側板邊 tab
            for ratio in [0.30, 0.70]:
                x = bx + board_w * ratio
                add_lwpolyline_rect(msp, x - tab_length / 2.0, by - tab_width / 2.0, tab_length, tab_width, layer)

            # 上側板邊 tab
            for ratio in [0.30, 0.70]:
                x = bx + board_w * ratio
                add_lwpolyline_rect(msp, x - tab_length / 2.0, by + board_h - tab_width / 2.0, tab_length, tab_width, layer)


def draw_outer_edge_tabs(
    msp,
    panel_x0: float,
    panel_y0: float,
    rail_left: float,
    rail_bottom: float,
    board_w: float,
    board_h: float,
    pitch_x: float,
    pitch_y: float,
    columns: int,
    rows: int,
    tab_length: float,
    tab_width: float,
    layer: str = "PANEL_TAB",
):
    """
    只在外側板邊與工藝邊交界處規劃 Router / Tab 連接點。

    使用時機：
    1. 保留上一版 v0.9.1 的板與板之間 Router / Tab 畫法。
    2. 額外補上外側板邊 Tab 點。
    3. 不重複畫內部 seam 的 Tab，避免格式變亂。
    4. Tab 以白色線規劃，實際尺寸與切點間距需由 ME/CAM 最終確認。
    """
    ratios = [0.30, 0.70]

    for row in range(rows):
        by = panel_y0 + rail_bottom + row * pitch_y

        # 左外側板邊：第一欄左側
        bx_left = panel_x0 + rail_left
        for ratio in ratios:
            y = by + board_h * ratio
            add_lwpolyline_rect(
                msp,
                bx_left - tab_width / 2.0,
                y - tab_length / 2.0,
                tab_width,
                tab_length,
                layer,
            )

        # 右外側板邊：最後一欄右側
        bx_right = panel_x0 + rail_left + (columns - 1) * pitch_x + board_w
        for ratio in ratios:
            y = by + board_h * ratio
            add_lwpolyline_rect(
                msp,
                bx_right - tab_width / 2.0,
                y - tab_length / 2.0,
                tab_width,
                tab_length,
                layer,
            )

    for col in range(columns):
        bx = panel_x0 + rail_left + col * pitch_x

        # 下外側板邊：第一列下側
        by_bottom = panel_y0 + rail_bottom
        for ratio in ratios:
            x = bx + board_w * ratio
            add_lwpolyline_rect(
                msp,
                x - tab_length / 2.0,
                by_bottom - tab_width / 2.0,
                tab_length,
                tab_width,
                layer,
            )

        # 上外側板邊：最後一列上側
        by_top = panel_y0 + rail_bottom + (rows - 1) * pitch_y + board_h
        for ratio in ratios:
            x = bx + board_w * ratio
            add_lwpolyline_rect(
                msp,
                x - tab_length / 2.0,
                by_top - tab_width / 2.0,
                tab_length,
                tab_width,
                layer,
            )


# =========================================================
# 報告文字
# =========================================================

def build_feature_summary_text(panel_features: Dict[str, Any]) -> str:
    if not panel_features:
        return "尚未取得 Fiducial / Tooling Hole 自動配置資訊。"

    fiducials = panel_features.get("fiducials", [])
    tooling_holes = panel_features.get("tooling_holes", [])
    warnings = panel_features.get("warnings", [])
    notes = panel_features.get("notes", [])

    fid_lines = []
    for f in fiducials:
        fid_lines.append(f"- {f.get('name')}：({f.get('x')}, {f.get('y')}) mm，直徑 {f.get('diameter_mm')} mm，位置 {f.get('location')}")

    tooling_lines = []
    for h in tooling_holes:
        tooling_lines.append(f"- {h.get('name')}：({h.get('x')}, {h.get('y')}) mm，直徑 {h.get('diameter_mm')} mm，位置 {h.get('location')}")

    warning_lines = [f"- {w}" for w in warnings]
    note_lines = [f"- {n}" for n in notes]

    return f"""
配置規則：{panel_features.get("rule_summary", "")}

Fiducial 配置：
{chr(10).join(fid_lines) if fid_lines else "- 無"}

Tooling Hole 配置：
{chr(10).join(tooling_lines) if tooling_lines else "- 無"}

注意事項：
{chr(10).join(note_lines) if note_lines else "- 依公司連板規範配置"}

警告：
{chr(10).join(warning_lines) if warning_lines else "- 無重大警告"}
""".strip()


def build_strategy_summary_text(strategy: Optional[Dict[str, Any]], strategy_applied: bool, strategy_parse_error: Optional[str]) -> str:
    if strategy_applied and strategy:
        return f"""
- 策略套用狀態：已套用
- 策略來源：{strategy.get("strategy_source", PANEL_RULES["source"])}
- 程式規則版本：{PANEL_RULES["version"]}
- 板邊策略：{strategy.get("rail_policy", "four_sides")}
- 建議分板方式：{strategy.get("recommended_method", "")}
- Layout：{json.dumps(strategy.get("layout", {}), ensure_ascii=False)}
- Fiducial Rule：{json.dumps(strategy.get("fiducial_rule", {}), ensure_ascii=False)}
- Tooling Hole Rule：{json.dumps(strategy.get("tooling_hole_rule", {}), ensure_ascii=False)}
""".strip()

    if strategy_parse_error:
        return f"""
- 策略套用狀態：已改用程式內建公司規範
- Dify strategy_json 解析錯誤：{strategy_parse_error}
""".strip()

    return """
- 策略套用狀態：已套用程式內建公司規範
""".strip()


def build_ai_report_markdown(
    product_name: str,
    object_key: str,
    result: Dict[str, Any],
    panel_dxf: Optional[Dict[str, Any]] = None,
    strategy: Optional[Dict[str, Any]] = None,
    strategy_applied: bool = False,
    strategy_parse_error: Optional[str] = None,
) -> str:
    best = result["best_candidate"]
    comparison_table = build_comparison_table_markdown(result.get("display_candidates", result["all_candidates"]), limit=3)
    caution_text = build_caution_and_not_summary(result.get("display_candidates", result["all_candidates"]), limit=3)

    dxf_info = ""
    feature_text = ""

    if panel_dxf:
        geometry_info = panel_dxf.get("geometry_info", {}) or {}
        panel_features = geometry_info.get("panel_features", {}) or {}
        feature_text = build_feature_summary_text(panel_features)
        dxf_info = f"""
- 輸出 DXF 檔名：{panel_dxf.get("output_filename", "")}
- 輸出 object_key：{panel_dxf.get("output_object_key", "")}
- 下載連結：{panel_dxf.get("download_url", "")}
- 幾何產出模式：{panel_dxf.get("geometry_mode", "")}
- 程式規則版本：{panel_dxf.get("program_rule_version", "")}
- 策略來源：{panel_dxf.get("strategy_source", "")}
""".strip()

    strategy_text = build_strategy_summary_text(strategy, strategy_applied, strategy_parse_error)

    report = f"""
# PCB 連版規劃 AI 建議報告

## 一、AI 建議結論

- 產品名稱：{product_name}
- 原始 DXF object_key：{object_key}
- 建議連版方式：{best["panel_type"]}
- 建議 Panel 尺寸：{best["panel_size"]}
- 每 Panel 數量：{best["pcs_per_panel"]} pcs/panel
- 建議分板方式：{best["split_method"]}
- 板邊策略：{best.get("rail_policy", "four_sides")}
- 風險分數：{best["risk_score"]}
- 風險等級：{best["risk_level_zh"]}
- 狀態：{best["status_zh"]}
- 是否可進入下一階段：{"可進入下一階段，但仍需 ME / CAM 工程師確認" if best["status_code"] == "recommended" else "需先由 ME / CAM 工程師審查後再決定"}

## 二、前三個候選方案比較表

{comparison_table}

## 三、公司連板規範套用說明

{strategy_text}

## 四、推薦方案說明

本次系統推薦方案為 {best["panel_type"]}，Panel 尺寸為 {best["panel_size"]}，每 Panel 可生產 {best["pcs_per_panel"]} pcs，建議分板方式為 {best["split_method"]}。此方案風險分數為 {best["risk_score"]}，風險等級為「{best["risk_level_zh"]}」，狀態為「{best["status_zh"]}」。主要判斷原因：{best["reason_text"]}

## 五、前三個候選方案中，謹慎使用與不建議方案原因

{caution_text}

## 六、DXF 輸出資訊

{dxf_info if dxf_info else "DXF 已由系統產生，請於下載連結取得。"}

## 七、Fiducial / Tooling Hole 自動配置

{feature_text if feature_text else "尚未取得 Fiducial / Tooling Hole 配置資訊。"}

## 八、製程風險提醒

- 光學點及工具孔需距離板邊 5 mm 以上。
- 板邊需判斷兩側或四側；若需治具定位、Fiducial 或 Router Tab，採四邊工藝邊。
- 板邊、連接點、光學點與定位點需以白色線規劃。
- 板邊建議 8～10 mm，若輸入值較小，需由 ME/CAM 確認。
- ROUTE 切點間距至少 2 mm 以上。
- 零件與 V-CUT 線距離需大於 2 mm。
- V-CUT 線邊 5 mm 內，零件高度不可超過 10 mm。
- MLCC 0805 以上若距板邊 5 mm 內，應使用 Router 製程。
- 金手指周圍不可設計連接點和 V-CUT 線。
- 若有 BGA/QFN、DIP、重零件或異形板，需優先檢討 Router / Tab、治具支撐與分板應力。

## 九、ME / CAM 最終確認清單

{chr(10).join(["- " + item for item in PANEL_RULES["check_items"]])}

## 十、輸出限制說明

本階段輸出的 DXF 為 AI 建議版，正式投產前仍需 ME / CAM 工程師確認 V-CUT、Router、Fiducial、Tooling Hole、白色雷雕框、Tab 連接點、分板應力與正式 CAM 檔案。
"""
    return report.strip()


# =========================================================
# DXF 產生
# =========================================================

def create_real_panel_dxf_from_source(
    source_path: str,
    output_path: str,
    product_name: str,
    single_board_length: float,
    single_board_width: float,
    rail_width: float,
    candidate: Dict[str, Any],
    has_bga_qfn: bool = False,
    has_dip: bool = False,
    has_heavy_component: bool = False,
    is_irregular_shape: bool = False,
    strategy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    ME 連板圖模板式 DXF 產生器。

    本版重點：
    1. 板邊判斷兩側或四側，若需 Tooling/Fiducial/Router Tab 則用四側。
    2. 板邊、連接點、光學點、定位點、尺寸標註與引線皆用白色線規劃。
    3. 板與板之間及板邊工藝邊皆畫 Tab 連接點。
    4. 尺寸標註包含總長、單板寬、有效寬、總高、單板高、板邊距離、孔點引線。
    5. Tooling Hole 與 Fiducial 具清楚引線標註。
    """

    doc = ezdxf.readfile(source_path)
    msp = doc.modelspace()

    # 所有新增規劃線一律使用白色線，符合使用者要求。
    white_layers = [
        "PANEL_OUTLINE", "PANEL_VCUT", "PANEL_ROUTE", "PANEL_TAB",
        "PANEL_TOOLING", "PANEL_FIDUCIAL", "PANEL_TEXT", "PANEL_NOTE",
        "PANEL_WARNING", "PANEL_DIMENSION", "PANEL_FRAME", "PANEL_TITLE",
        "PANEL_RAIL", "PANEL_LEADER",
    ]
    for layer_name in white_layers:
        ensure_layer(doc, layer_name, 7)

    detected_bbox = get_modelspace_bbox(doc)
    if detected_bbox:
        min_x, min_y, max_x, max_y = detected_bbox
        detected_board_w = max_x - min_x
        detected_board_h = max_y - min_y
    else:
        min_x = 0.0
        min_y = 0.0
        detected_board_w = single_board_length
        detected_board_h = single_board_width

    board_w = detected_board_w if detected_board_w > 0 else single_board_length
    board_h = detected_board_h if detected_board_h > 0 else single_board_width

    columns = int(candidate.get("x_count", candidate.get("columns", 1)))
    rows = int(candidate.get("y_count", candidate.get("rows", 1)))
    split_method = str(candidate.get("split_method", "Router / Tab"))

    rail_policy = str(candidate.get("rail_policy", "four_sides"))
    if strategy:
        rail_policy = str(strategy.get("rail_policy", rail_policy))
    if rail_policy not in ["four_sides", "two_sides"]:
        rail_policy = "four_sides"

    gap_x = float(candidate.get("gap_x_mm", PANEL_RULES["cutting"]["router_min_gap_mm"]))
    gap_y = float(candidate.get("gap_y_mm", PANEL_RULES["cutting"]["router_min_gap_mm"]))

    # ROUTE 各連板間距至少 2 mm 以上。
    # 即使 Dify strategy_json 或 candidate 傳入較小 gap，Router / Tab 製程仍強制修正為公司規範最小值。
    if "Router" in split_method or "Tab" in split_method:
        min_route_gap = PANEL_RULES["cutting"]["router_min_gap_mm"]
        gap_x = max(gap_x, min_route_gap)
        gap_y = max(gap_y, min_route_gap)

    input_rail_left = float(candidate.get("rail_left_mm", PANEL_RULES["rail"]["default_mm"]))
    input_rail_right = float(candidate.get("rail_right_mm", PANEL_RULES["rail"]["default_mm"]))
    input_rail_top = float(candidate.get("rail_top_mm", PANEL_RULES["rail"]["default_mm"]))
    input_rail_bottom = float(candidate.get("rail_bottom_mm", PANEL_RULES["rail"]["default_mm"]))

    min_template_rail = PANEL_RULES["rail"]["company_recommended_min_mm"]
    rail_left = max(input_rail_left, min_template_rail)
    rail_right = max(input_rail_right, min_template_rail)

    if rail_policy == "four_sides":
        rail_top = max(input_rail_top, min_template_rail)
        rail_bottom = max(input_rail_bottom, min_template_rail)
    else:
        rail_top = 0.0
        rail_bottom = 0.0

    pitch_x = board_w + gap_x
    pitch_y = board_h + gap_y

    panel_w = rail_left + columns * board_w + max(columns - 1, 0) * gap_x + rail_right
    panel_h = rail_bottom + rows * board_h + max(rows - 1, 0) * gap_y + rail_top
    pcs_per_panel = columns * rows

    # 圖框空間，預留上方說明、右側引線、下方標題欄。
    frame_w = max(panel_w + 190.0, 340.0)
    frame_h = max(panel_h + 155.0, 240.0)
    frame_x0 = 0.0
    frame_y0 = 0.0
    frame_x1 = frame_x0 + frame_w
    frame_y1 = frame_y0 + frame_h
    inner_margin = 8.0
    bottom_title_height = 32.0

    panel_x0 = frame_x0 + (frame_w - panel_w) / 2.0 - 12.0
    panel_y0 = frame_y0 + bottom_title_height + 34.0

    original_entities = list(msp)

    # 圖框與外框
    add_lwpolyline_rect(msp, frame_x0, frame_y0, frame_w, frame_h, "PANEL_FRAME")
    add_lwpolyline_rect(msp, frame_x0 + inner_margin, frame_y0 + inner_margin, frame_w - inner_margin * 2, frame_h - inner_margin * 2, "PANEL_FRAME")

    grid_count_x = 14
    grid_count_y = 8
    usable_w = frame_w - inner_margin * 2
    usable_h = frame_h - inner_margin * 2

    for i in range(1, grid_count_x):
        x = frame_x0 + inner_margin + i * usable_w / grid_count_x
        add_line(msp, x, frame_y0 + inner_margin, x, frame_y0 + inner_margin + 3.0, "PANEL_FRAME")
        add_line(msp, x, frame_y1 - inner_margin, x, frame_y1 - inner_margin - 3.0, "PANEL_FRAME")

    for i in range(1, grid_count_y):
        y = frame_y0 + inner_margin + i * usable_h / grid_count_y
        add_line(msp, frame_x0 + inner_margin, y, frame_x0 + inner_margin + 3.0, y, "PANEL_FRAME")
        add_line(msp, frame_x1 - inner_margin, y, frame_x1 - inner_margin - 3.0, y, "PANEL_FRAME")

    # 上方說明區
    note_top_y = frame_y1 - 20.0
    left_note_x = frame_x0 + 22.0
    center_note_x = frame_x0 + frame_w * 0.42
    right_note_x = frame_x0 + frame_w * 0.68

    add_text(msp, "white silk block規則如下", left_note_x, note_top_y, 2.2, "PANEL_NOTE")
    add_text(msp, "1. 白色框不可塗滿印刷", left_note_x, note_top_y - 5.0, 1.8, "PANEL_NOTE")
    add_text(msp, "2. 尺寸 4.5*30mm 或 5*30mm", left_note_x, note_top_y - 10.0, 1.8, "PANEL_NOTE")
    add_text(msp, "3. 正反面右側及對角需設計", left_note_x, note_top_y - 15.0, 1.8, "PANEL_NOTE")
    add_text(msp, "4. 倍留補件放置圖", left_note_x, note_top_y - 20.0, 1.8, "PANEL_NOTE")
    add_text(msp, "5. 不可塗到光學點", left_note_x, note_top_y - 25.0, 1.8, "PANEL_NOTE")

    add_text(msp, "Unit:mm", center_note_x, note_top_y, 2.0, "PANEL_NOTE")
    add_text(msp, f"One piece={board_w:.2f} x {board_h:.2f} mm", center_note_x, note_top_y - 5.0, 2.0, "PANEL_NOTE")
    add_text(msp, f"Panel size={panel_w:.2f} x {panel_h:.2f} mm", center_note_x, note_top_y - 10.0, 2.0, "PANEL_NOTE")
    add_text(msp, f"{pcs_per_panel} pcs/panel", center_note_x, note_top_y - 15.0, 2.0, "PANEL_NOTE")
    add_text(msp, f"Rail policy={rail_policy}", center_note_x, note_top_y - 20.0, 2.0, "PANEL_NOTE")

    process_text = "No V-CUT and stamp hole" if ("Router" in split_method or "Tab" in split_method) else "V-CUT process"
    add_text(msp, process_text, center_note_x, note_top_y - 25.0, 2.0, "PANEL_NOTE")

    add_text(msp, "PS: 判板或過爐方向之設定由製造建議為準", right_note_x, note_top_y, 1.8, "PANEL_WARNING")
    add_text(msp, "EQ：一條線或鋼網尺寸管制依製程檢核", right_note_x, note_top_y - 5.0, 1.8, "PANEL_WARNING")
    add_text(msp, "板邊、連接點、光學點、定位點皆以白色線規劃", right_note_x, note_top_y - 12.0, 1.8, "PANEL_NOTE")
    add_text(msp, "若採 ROUTE，不可製作 V-CUT 線和郵票孔", right_note_x, note_top_y - 17.0, 1.8, "PANEL_NOTE")

    # 複製原始單板
    base_dx = panel_x0 + rail_left - min_x
    base_dy = panel_y0 + rail_bottom - min_y
    base_matrix = Matrix44.translate(base_dx, base_dy, 0)

    for entity in original_entities:
        transform_entity_safe(entity, base_matrix)

    for row in range(rows):
        for col in range(columns):
            if row == 0 and col == 0:
                continue
            dx = col * pitch_x
            dy = row * pitch_y
            copy_matrix = Matrix44.translate(dx, dy, 0)
            for entity in original_entities:
                try:
                    copied = entity.copy()
                    transform_entity_safe(copied, copy_matrix)
                    msp.add_entity(copied)
                except Exception:
                    continue

    # Panel 外框、工藝邊與每片單板外框
    add_lwpolyline_rect(msp, panel_x0, panel_y0, panel_w, panel_h, "PANEL_OUTLINE")

    # 工藝邊邊界線：清楚標示四側或兩側。
    if rail_left > 0:
        add_line(msp, panel_x0 + rail_left, panel_y0, panel_x0 + rail_left, panel_y0 + panel_h, "PANEL_RAIL")
    if rail_right > 0:
        add_line(msp, panel_x0 + panel_w - rail_right, panel_y0, panel_x0 + panel_w - rail_right, panel_y0 + panel_h, "PANEL_RAIL")
    if rail_top > 0:
        add_line(msp, panel_x0, panel_y0 + panel_h - rail_top, panel_x0 + panel_w, panel_y0 + panel_h - rail_top, "PANEL_RAIL")
    if rail_bottom > 0:
        add_line(msp, panel_x0, panel_y0 + rail_bottom, panel_x0 + panel_w, panel_y0 + rail_bottom, "PANEL_RAIL")

    for row in range(rows):
        for col in range(columns):
            board_x = panel_x0 + rail_left + col * pitch_x
            board_y = panel_y0 + rail_bottom + row * pitch_y
            add_lwpolyline_rect(msp, board_x, board_y, board_w, board_h, "PANEL_OUTLINE")

    # Router / Tab 或 V-cut
    # -----------------------------------------------------
    # 依上一版 0.9.1 的 Router / Tab 畫法作為內部 seam 基礎：
    # 1. Router channel 畫在板與板之間。
    # 2. 垂直 seam：每片板邊放上下兩個 Tab。
    # 3. 水平 seam：每片板邊放左右兩個 Tab。
    # 4. 另補外側板邊 Router / Tab 連接點，且 ROUTE 各連板間距至少 2 mm。
    # -----------------------------------------------------
    if "V-cut" in split_method or "V-CUT" in split_method:
        for col in range(1, columns):
            x = panel_x0 + rail_left + col * board_w + (col - 0.5) * gap_x
            add_line(msp, x, panel_y0 + rail_bottom, x, panel_y0 + panel_h - rail_top, "PANEL_VCUT")

        for row in range(1, rows):
            y = panel_y0 + rail_bottom + row * board_h + (row - 0.5) * gap_y
            add_line(msp, panel_x0 + rail_left, y, panel_x0 + panel_w - rail_right, y, "PANEL_VCUT")
    else:
        router_gap = max(gap_x, PANEL_RULES["cutting"]["router_min_gap_mm"])
        route_width = max(router_gap, 2.0)

        tab_length = 7.0
        tab_width = 2.0

        # 垂直板間 Router channel
        for col in range(1, columns):
            x_center = panel_x0 + rail_left + col * board_w + (col - 0.5) * gap_x
            x_route = x_center - route_width / 2.0

            add_lwpolyline_rect(
                msp,
                x_route,
                panel_y0 + rail_bottom,
                route_width,
                panel_h - rail_top - rail_bottom,
                "PANEL_ROUTE",
            )

            for row in range(rows):
                board_y = panel_y0 + rail_bottom + row * pitch_y

                tab_y_1 = board_y + board_h * 0.30
                tab_y_2 = board_y + board_h * 0.70

                add_lwpolyline_rect(
                    msp,
                    x_center - tab_width / 2.0,
                    tab_y_1 - tab_length / 2.0,
                    tab_width,
                    tab_length,
                    "PANEL_TAB",
                )

                add_lwpolyline_rect(
                    msp,
                    x_center - tab_width / 2.0,
                    tab_y_2 - tab_length / 2.0,
                    tab_width,
                    tab_length,
                    "PANEL_TAB",
                )

        # 水平板間 Router channel
        for row in range(1, rows):
            y_center = panel_y0 + rail_bottom + row * board_h + (row - 0.5) * gap_y
            y_route = y_center - route_width / 2.0

            add_lwpolyline_rect(
                msp,
                panel_x0 + rail_left,
                y_route,
                panel_w - rail_left - rail_right,
                route_width,
                "PANEL_ROUTE",
            )

            for col in range(columns):
                board_x = panel_x0 + rail_left + col * pitch_x

                tab_x_1 = board_x + board_w * 0.30
                tab_x_2 = board_x + board_w * 0.70

                add_lwpolyline_rect(
                    msp,
                    tab_x_1 - tab_length / 2.0,
                    y_center - tab_width / 2.0,
                    tab_length,
                    tab_width,
                    "PANEL_TAB",
                )

                add_lwpolyline_rect(
                    msp,
                    tab_x_2 - tab_length / 2.0,
                    y_center - tab_width / 2.0,
                    tab_length,
                    tab_width,
                    "PANEL_TAB",
                )

        # 外側板邊 Router / Tab 連接點：補上板邊與工藝邊交界處的 Tab。
        # 內部 seam 仍維持上一版 v0.9.1 畫法。
        draw_outer_edge_tabs(
            msp=msp,
            panel_x0=panel_x0,
            panel_y0=panel_y0,
            rail_left=rail_left,
            rail_bottom=rail_bottom,
            board_w=board_w,
            board_h=board_h,
            pitch_x=pitch_x,
            pitch_y=pitch_y,
            columns=columns,
            rows=rows,
            tab_length=tab_length,
            tab_width=tab_width,
            layer="PANEL_TAB",
        )

    # Tooling Hole / Fiducial
    feature_result = auto_determine_panel_features(
        panel_w=panel_w,
        panel_h=panel_h,
        rail_width=max(min(rail_left, rail_right, rail_top if rail_top > 0 else rail_left, rail_bottom if rail_bottom > 0 else rail_left), 0.0),
        columns=columns,
        rows=rows,
        split_method=split_method,
        has_bga_qfn=has_bga_qfn,
        has_dip=has_dip,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
        strategy=strategy,
    )

    shifted_feature_result = dict(feature_result)
    shifted_tooling_holes = []
    for hole in feature_result.get("tooling_holes", []):
        h = dict(hole)
        h["x"] = round(float(h["x"]) + panel_x0, 3)
        h["y"] = round(float(h["y"]) + panel_y0, 3)
        shifted_tooling_holes.append(h)

    shifted_fiducials = []
    for fid in feature_result.get("fiducials", []):
        f = dict(fid)
        f["x"] = round(float(f["x"]) + panel_x0, 3)
        f["y"] = round(float(f["y"]) + panel_y0, 3)
        shifted_fiducials.append(f)

    shifted_feature_result["tooling_holes"] = shifted_tooling_holes
    shifted_feature_result["fiducials"] = shifted_fiducials

    draw_panel_features(msp=msp, feature_result=shifted_feature_result, tooling_layer="PANEL_TOOLING", fiducial_layer="PANEL_FIDUCIAL", text_layer="PANEL_TEXT")

    # 引線標註：以右下角附近一組 Tooling/Fiducial 為基準，類似模板。
    tooling_callout = PANEL_RULES.get("tooling_hole", {}).get("template_callout", "4-Ø3.05 ±0.05 Tooling hole")
    fiducial_callout = PANEL_RULES.get("fiducial", {}).get("template_callout", "3-Ø1.5 Fiducial Mark")

    # 找右下 Tooling 與附近 Fiducial 做引線，找不到則用 panel 右下。
    if shifted_tooling_holes:
        th_ref = sorted(shifted_tooling_holes, key=lambda h: (float(h["x"]), -float(h["y"])), reverse=True)[0]
        th_x = float(th_ref["x"])
        th_y = float(th_ref["y"])
    else:
        th_x = panel_x0 + panel_w - 5.0
        th_y = panel_y0 + 5.0

    if shifted_fiducials:
        fd_ref = sorted(shifted_fiducials, key=lambda f: abs(float(f["x"]) - th_x) + abs(float(f["y"]) - th_y))[0]
        fd_x = float(fd_ref["x"])
        fd_y = float(fd_ref["y"])
    else:
        fd_x = th_x - 7.7
        fd_y = th_y - 5.0

    callout_text_x = min(th_x + 32.0, frame_x1 - 85.0)
    callout_y1 = max(panel_y0 - 12.0, frame_y0 + 38.0)
    callout_y2 = callout_y1 - 10.0

    add_leader_callout(msp, th_x, th_y, th_x + 16.0, callout_y1, callout_text_x, callout_y1, tooling_callout, "PANEL_DIMENSION", 1.8)
    add_leader_callout(msp, fd_x, fd_y, fd_x + 18.0, callout_y2, callout_text_x, callout_y2, fiducial_callout, "PANEL_DIMENSION", 1.8)

    # 孔位細部尺寸標註，模擬 5.00 / 10.00 / 9.00 / 7.70。
    add_dimension_line(msp, th_x, th_y, th_x, th_y + 10.0, "10.00", th_x + 2.0, th_y + 4.5, "PANEL_DIMENSION", 1.8)
    add_dimension_line(msp, th_x - 5.0, th_y, th_x, th_y, "5.00", th_x - 4.0, th_y - 4.0, "PANEL_DIMENSION", 1.8)
    add_dimension_line(msp, fd_x, fd_y, th_x, th_y, "7.70", (fd_x + th_x) / 2.0 - 3.0, (fd_y + th_y) / 2.0 + 2.0, "PANEL_DIMENSION", 1.8)
    add_dimension_line(msp, fd_x, fd_y - 7.0, th_x, fd_y - 7.0, "9.00", (fd_x + th_x) / 2.0 - 3.0, fd_y - 10.0, "PANEL_DIMENSION", 1.8)

    # 尺寸標註：總長、單板寬、有效寬、總高、單板高、工藝邊。
    dim_top_y = panel_y0 + panel_h + 8.0
    dim_top_y_2 = dim_top_y + 6.0
    dim_top_y_3 = dim_top_y + 12.0
    dim_left_x = panel_x0 - 10.0
    dim_left_x_2 = dim_left_x - 7.0

    add_dimension_line(msp, panel_x0, dim_top_y, panel_x0 + panel_w, dim_top_y, f"{panel_w:.2f}", panel_x0 + panel_w / 2.0, dim_top_y + 1.5, "PANEL_DIMENSION")
    add_dimension_line(msp, panel_x0 + rail_left, dim_top_y_2, panel_x0 + rail_left + board_w, dim_top_y_2, f"{board_w:.2f}", panel_x0 + rail_left + board_w / 2.0, dim_top_y_2 + 1.5, "PANEL_DIMENSION")
    effective_w = columns * board_w + max(columns - 1, 0) * gap_x
    add_dimension_line(msp, panel_x0 + rail_left, dim_top_y_3, panel_x0 + rail_left + effective_w, dim_top_y_3, f"{effective_w:.2f}", panel_x0 + rail_left + effective_w / 2.0, dim_top_y_3 + 1.5, "PANEL_DIMENSION")

    add_dimension_line(msp, dim_left_x, panel_y0, dim_left_x, panel_y0 + panel_h, f"{panel_h:.2f}", dim_left_x - 9.0, panel_y0 + panel_h / 2.0, "PANEL_DIMENSION")
    add_dimension_line(msp, dim_left_x_2, panel_y0 + rail_bottom, dim_left_x_2, panel_y0 + rail_bottom + board_h, f"{board_h:.2f}", dim_left_x_2 - 10.0, panel_y0 + rail_bottom + board_h / 2.0, "PANEL_DIMENSION")

    # 工藝邊尺寸引線
    if rail_left > 0:
        add_dimension_line(msp, panel_x0, panel_y0 - 8.0, panel_x0 + rail_left, panel_y0 - 8.0, f"{rail_left:.2f}", panel_x0 + rail_left / 2.0 - 2.0, panel_y0 - 11.0, "PANEL_DIMENSION")
    if rail_bottom > 0:
        add_dimension_line(msp, panel_x0 - 18.0, panel_y0, panel_x0 - 18.0, panel_y0 + rail_bottom, f"{rail_bottom:.2f}", panel_x0 - 27.0, panel_y0 + rail_bottom / 2.0, "PANEL_DIMENSION")

    # Tab 尺寸引線，放在第一個板間或第一片板邊。
    tab_callout_x = panel_x0 + rail_left + board_w + gap_x / 2.0 if columns > 1 else panel_x0 + rail_left + board_w
    tab_callout_y = panel_y0 + rail_bottom + board_h * 0.70
    add_leader_callout(
        msp,
        tab_callout_x,
        tab_callout_y,
        tab_callout_x + 14.0,
        tab_callout_y + 18.0,
        tab_callout_x + 20.0,
        tab_callout_y + 18.0,
        f"TAB {tab_width:.2f} x {tab_length:.2f}, gap≥{PANEL_RULES['cutting']['router_min_gap_mm']:.2f}",
        "PANEL_DIMENSION",
        1.8,
    )

    # 標題欄
    title_w = 92.0
    title_h = 24.0
    title_x = frame_x1 - inner_margin - title_w
    title_y = frame_y0 + inner_margin

    add_lwpolyline_rect(msp, title_x, title_y, title_w, title_h, "PANEL_TITLE")
    add_line(msp, title_x, title_y + 6.0, title_x + title_w, title_y + 6.0, "PANEL_TITLE")
    add_line(msp, title_x, title_y + 12.0, title_x + title_w, title_y + 12.0, "PANEL_TITLE")
    add_line(msp, title_x, title_y + 18.0, title_x + title_w, title_y + 18.0, "PANEL_TITLE")
    add_line(msp, title_x + 20.0, title_y, title_x + 20.0, title_y + title_h, "PANEL_TITLE")
    add_line(msp, title_x + 54.0, title_y, title_x + 54.0, title_y + title_h, "PANEL_TITLE")

    add_text(msp, "DRAWN", title_x + 2.0, title_y + 19.0, 1.4, "PANEL_TITLE")
    add_text(msp, "CHECK", title_x + 2.0, title_y + 13.0, 1.4, "PANEL_TITLE")
    add_text(msp, "APPROVED", title_x + 2.0, title_y + 7.0, 1.4, "PANEL_TITLE")
    add_text(msp, "MODEL", title_x + 2.0, title_y + 1.0, 1.4, "PANEL_TITLE")
    add_text(msp, product_name, title_x + 22.0, title_y + 1.0, 1.5, "PANEL_TITLE")

    # 產圖資訊移到左下，不壓板子。
    info_x = frame_x0 + inner_margin + 8.0
    info_y = frame_y0 + inner_margin + 12.0
    strategy_source = strategy.get("strategy_source", PANEL_RULES["source"]) if strategy else PANEL_RULES["source"]

    add_text(msp, f"Product: {product_name}", info_x, info_y + 26.0, 1.7, "PANEL_TEXT")
    add_text(msp, f"One piece: {board_w:.2f} x {board_h:.2f} mm", info_x, info_y + 21.5, 1.7, "PANEL_TEXT")
    add_text(msp, f"Panel size: {panel_w:.2f} x {panel_h:.2f} mm", info_x, info_y + 17.0, 1.7, "PANEL_TEXT")
    add_text(msp, f"{pcs_per_panel} pcs/panel, Layout: {columns} x {rows}, Split: {split_method}", info_x, info_y + 12.5, 1.7, "PANEL_TEXT")
    add_text(msp, f"Rail: {rail_policy}, L/R/T/B={rail_left:.2f}/{rail_right:.2f}/{rail_top:.2f}/{rail_bottom:.2f}", info_x, info_y + 8.0, 1.7, "PANEL_TEXT")
    add_text(msp, f"Fiducial: {len(shifted_fiducials)} pcs, Tooling Hole: {len(shifted_tooling_holes)} pcs", info_x, info_y + 3.5, 1.7, "PANEL_TEXT")
    add_text(msp, f"Strategy used: True, Source: {strategy_source}", info_x, info_y - 1.0, 1.7, "PANEL_TEXT")

    doc.saveas(output_path)

    return {
        "detected_board_width_mm": round(board_w, 3),
        "detected_board_height_mm": round(board_h, 3),
        "panel_width_mm": round(panel_w, 3),
        "panel_height_mm": round(panel_h, 3),
        "columns": columns,
        "rows": rows,
        "pcs_per_panel": pcs_per_panel,
        "gap_x_mm": gap_x,
        "gap_y_mm": gap_y,
        "rail_left_mm": rail_left,
        "rail_right_mm": rail_right,
        "rail_top_mm": rail_top,
        "rail_bottom_mm": rail_bottom,
        "rail_policy": rail_policy,
        "split_method": split_method,
        "panel_features": shifted_feature_result,
        "template_style": "ME_panel_drawing_template_v4_detail_edge_routertab",
        "template_notes": {
            "white_line_planning": True,
            "rail_policy": rail_policy,
            "edge_tabs": "board-to-board tabs keep v0.9.1 logic; board-to-rail outer edge tabs are added with white lines",
            "route_gap_rule": "ROUTE 各連板間距至少 2.0 mm 以上",
            "leader_dimensions": "dimension leaders and callouts are added for tooling hole, fiducial, tab, rail and panel size",
            "tooling_hole_callout": tooling_callout,
            "fiducial_callout": fiducial_callout,
            "candidate_layout": f"{columns}x{rows} is kept from system recommendation",
        },
    }


def create_simple_panel_dxf_fallback(
    output_path: str,
    product_name: str,
    single_board_length: float,
    single_board_width: float,
    rail_width: float,
    candidate: Dict[str, Any],
    has_bga_qfn: bool = False,
    has_dip: bool = False,
    has_heavy_component: bool = False,
    is_irregular_shape: bool = False,
    strategy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    for layer_name in ["PANEL_OUTLINE", "PANEL_VCUT", "PANEL_ROUTE", "PANEL_TAB", "PANEL_TOOLING", "PANEL_FIDUCIAL", "PANEL_TEXT", "PANEL_DIMENSION"]:
        ensure_layer(doc, layer_name, 7)

    columns = int(candidate.get("x_count", 1))
    rows = int(candidate.get("y_count", 1))
    gap_x = float(candidate.get("gap_x_mm", 0.0))
    gap_y = float(candidate.get("gap_y_mm", 0.0))
    rail_left = float(candidate.get("rail_left_mm", rail_width))
    rail_right = float(candidate.get("rail_right_mm", rail_width))
    rail_top = float(candidate.get("rail_top_mm", rail_width))
    rail_bottom = float(candidate.get("rail_bottom_mm", rail_width))

    panel_w = rail_left + rail_right + single_board_length * columns + gap_x * max(columns - 1, 0)
    panel_h = rail_top + rail_bottom + single_board_width * rows + gap_y * max(rows - 1, 0)
    pitch_x = single_board_length + gap_x
    pitch_y = single_board_width + gap_y

    add_lwpolyline_rect(msp, 0, 0, panel_w, panel_h, "PANEL_OUTLINE")

    for row in range(rows):
        for col in range(columns):
            x = rail_left + col * pitch_x
            y = rail_bottom + row * pitch_y
            add_lwpolyline_rect(msp, x, y, single_board_length, single_board_width, "PANEL_OUTLINE")

    split_method = candidate.get("split_method", "V-cut")

    # ROUTE 各連板間距至少 2 mm 以上。
    if "Router" in str(split_method) or "Tab" in str(split_method):
        min_route_gap = PANEL_RULES["cutting"]["router_min_gap_mm"]
        gap_x = max(gap_x, min_route_gap)
        gap_y = max(gap_y, min_route_gap)

    tab_length = PANEL_RULES["tab"]["tab_length_mm"]
    tab_width = PANEL_RULES["tab"]["tab_width_mm"]

    if "V-cut" in split_method or "V-CUT" in split_method:
        for col in range(1, columns):
            x = rail_left + col * single_board_length + (col - 0.5) * gap_x
            add_line(msp, x, rail_bottom, x, panel_h - rail_top, "PANEL_VCUT")
        for row in range(1, rows):
            y = rail_bottom + row * single_board_width + (row - 0.5) * gap_y
            add_line(msp, rail_left, y, panel_w - rail_right, y, "PANEL_VCUT")
    else:
        for col in range(1, columns):
            x1 = rail_left + col * single_board_length + (col - 1) * gap_x
            add_lwpolyline_rect(msp, x1, rail_bottom, max(gap_x, 0.3), panel_h - rail_top - rail_bottom, "PANEL_ROUTE")
        for row in range(1, rows):
            y1 = rail_bottom + row * single_board_width + (row - 1) * gap_y
            add_lwpolyline_rect(msp, rail_left, y1, panel_w - rail_left - rail_right, max(gap_y, 0.3), "PANEL_ROUTE")

    draw_edge_tabs(
        msp=msp,
        panel_x0=0,
        panel_y0=0,
        rail_left=rail_left,
        rail_bottom=rail_bottom,
        board_w=single_board_length,
        board_h=single_board_width,
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        columns=columns,
        rows=rows,
        tab_length=tab_length,
        tab_width=tab_width,
        layer="PANEL_TAB",
    )

    feature_result = auto_determine_panel_features(
        panel_w=panel_w,
        panel_h=panel_h,
        rail_width=min(rail_left, rail_right, rail_top, rail_bottom),
        columns=columns,
        rows=rows,
        split_method=split_method,
        has_bga_qfn=has_bga_qfn,
        has_dip=has_dip,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
        strategy=strategy,
    )

    draw_panel_features(msp=msp, feature_result=feature_result)

    strategy_source = strategy.get("strategy_source", PANEL_RULES["source"]) if strategy else PANEL_RULES["source"]
    add_text(msp, f"Product: {product_name}", 0, panel_h + 8, 2.5, "PANEL_TEXT")
    add_text(msp, f"Panel: {columns} x {rows}, {columns * rows} pcs", 0, panel_h + 4, 2.5, "PANEL_TEXT")
    add_text(msp, f"Fiducial: {feature_result.get('fiducial_count')} pcs, Tooling Hole: {feature_result.get('tooling_hole_count')} pcs", 0, panel_h, 2.5, "PANEL_TEXT")
    add_text(msp, f"Strategy used: True, Source: {strategy_source}", 0, panel_h - 4, 2.5, "PANEL_TEXT")
    add_text(msp, f"Program rule: {PANEL_RULES['version']}", 0, panel_h - 8, 2.5, "PANEL_TEXT")

    doc.saveas(output_path)

    return {
        "detected_board_width_mm": single_board_length,
        "detected_board_height_mm": single_board_width,
        "panel_width_mm": round(panel_w, 3),
        "panel_height_mm": round(panel_h, 3),
        "columns": columns,
        "rows": rows,
        "pcs_per_panel": columns * rows,
        "gap_x_mm": gap_x,
        "gap_y_mm": gap_y,
        "rail_left_mm": rail_left,
        "rail_right_mm": rail_right,
        "rail_top_mm": rail_top,
        "rail_bottom_mm": rail_bottom,
        "split_method": split_method,
        "panel_features": feature_result,
        "fallback": True,
    }


# =========================================================
# 本地產出
# =========================================================

def generate_local_panelization_outputs(inputs: Dict[str, str], strategy_json: str = "") -> Dict[str, Any]:
    object_key = str(inputs.get("object_key", ""))
    product_name = str(inputs.get("product_name", "UNKNOWN"))

    single_board_length = to_float(inputs.get("single_board_length", 120), 120)
    single_board_width = to_float(inputs.get("single_board_width", 80), 80)
    rail_width = to_float(inputs.get("rail_width", PANEL_RULES["rail"]["default_mm"]), PANEL_RULES["rail"]["default_mm"])
    smt_max_length = to_float(inputs.get("smt_max_length", 330), 330)
    smt_max_width = to_float(inputs.get("smt_max_width", 250), 250)
    ict_max_length = to_float(inputs.get("ict_max_length", 350), 350)
    ict_max_width = to_float(inputs.get("ict_max_width", 300), 300)

    has_bga_qfn = yes_no_to_bool(inputs.get("has_bga_qfn", "No"))
    has_dip = yes_no_to_bool(inputs.get("has_dip", "No"))
    has_heavy_component = yes_no_to_bool(inputs.get("has_heavy_component", "No"))
    is_irregular_shape = yes_no_to_bool(inputs.get("is_irregular_shape", "No"))

    local_input = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_source.dxf")
    download_file_from_minio(object_key, local_input)

    detected_length = single_board_length
    detected_width = single_board_width

    try:
        source_doc = ezdxf.readfile(local_input)
        detected_bbox = get_modelspace_bbox(source_doc)
        if detected_bbox:
            min_x, min_y, max_x, max_y = detected_bbox
            detected_length = max_x - min_x
            detected_width = max_y - min_y
            if detected_length <= 0:
                detected_length = single_board_length
            if detected_width <= 0:
                detected_width = single_board_width
    except Exception:
        detected_length = single_board_length
        detected_width = single_board_width

    result = calculate_candidates(
        detected_length,
        detected_width,
        rail_width,
        smt_max_length,
        smt_max_width,
        ict_max_length,
        ict_max_width,
        has_bga_qfn,
        has_dip,
        has_heavy_component,
        is_irregular_shape,
    )

    strategy, strategy_applied, strategy_parse_error = get_strategy_for_backend(
        strategy_json=strategy_json,
        best_candidate=result["best_candidate"],
        has_bga_qfn=has_bga_qfn,
        has_dip=has_dip,
        has_heavy_component=has_heavy_component,
        is_irregular_shape=is_irregular_shape,
        input_rail_width=rail_width,
    )

    candidate = apply_strategy_to_candidate(
        base_candidate=result["best_candidate"],
        strategy=strategy,
        board_w=detected_length,
        board_h=detected_width,
        default_rail_width=rail_width,
    )

    result = update_result_with_strategy_candidate(result, candidate)

    safe_name = safe_filename_name(product_name)
    output_name = f"panelized_{safe_name}_{candidate['panel_type']}_real.dxf"
    output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{output_name}")

    geometry_info = {}

    try:
        geometry_info = create_real_panel_dxf_from_source(
            source_path=local_input,
            output_path=output_path,
            product_name=product_name,
            single_board_length=detected_length,
            single_board_width=detected_width,
            rail_width=rail_width,
            candidate=candidate,
            has_bga_qfn=has_bga_qfn,
            has_dip=has_dip,
            has_heavy_component=has_heavy_component,
            is_irregular_shape=is_irregular_shape,
            strategy=strategy,
        )
        geometry_mode = "real_source_dxf_copied_me_template_detail"
    except Exception as e:
        geometry_info = create_simple_panel_dxf_fallback(
            output_path=output_path,
            product_name=product_name,
            single_board_length=detected_length,
            single_board_width=detected_width,
            rail_width=rail_width,
            candidate=candidate,
            has_bga_qfn=has_bga_qfn,
            has_dip=has_dip,
            has_heavy_component=has_heavy_component,
            is_irregular_shape=is_irregular_shape,
            strategy=strategy,
        )
        geometry_info["real_dxf_error"] = str(e)
        geometry_mode = "simple_fallback"

    try:
        os.remove(local_input)
    except Exception:
        pass

    output_object_key = f"outputs/{uuid.uuid4()}/{output_name}"

    upload_file_to_minio(local_path=output_path, object_name=output_object_key, content_type="application/dxf")

    try:
        os.remove(output_path)
    except Exception:
        pass

    minio_presigned_url = get_presigned_download_url(output_object_key, hours=24)
    api_download_url = make_public_api_download_url(output_object_key)

    panel_dxf = {
        "status": "success",
        "product_name": product_name,
        "input_object_key": object_key,
        "output_object_key": output_object_key,
        "output_filename": output_name,
        "download_url": api_download_url,
        "api_download_url": api_download_url,
        "minio_presigned_url": minio_presigned_url,
        "expires_hours": 24,
        "geometry_mode": geometry_mode,
        "geometry_info": geometry_info,
        "best_candidate": candidate,
        "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"],
        "candidates": result["candidates"],
        "strategy_applied": True,
        "strategy_source": strategy.get("strategy_source", PANEL_RULES["source"]),
        "strategy_json": strategy,
        "strategy_parse_error": strategy_parse_error,
        "program_rules_applied": True,
        "program_rule_version": PANEL_RULES["version"],
        "message": "Panelized DXF generated by real DXF panelization engine with embedded company panel rules and detailed ME template layout.",
    }

    report_text = build_ai_report_markdown(
        product_name=product_name,
        object_key=object_key,
        result=result,
        panel_dxf=panel_dxf,
        strategy=strategy,
        strategy_applied=strategy_applied,
        strategy_parse_error=strategy_parse_error,
    )

    return {
        "report_text": report_text,
        "panel_dxf": panel_dxf,
        "panel_dxf_info": panel_dxf,
        "output_object_key": output_object_key,
        "output_filename": output_name,
        "download_url": api_download_url,
        "api_download_url": api_download_url,
        "minio_presigned_url": minio_presigned_url,
        "best_candidate": candidate,
        "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"],
        "candidates": result["candidates"],
        "geometry_info": geometry_info,
        "geometry_mode": geometry_mode,
        "strategy_applied": True,
        "strategy_source": strategy.get("strategy_source", PANEL_RULES["source"]),
        "strategy_json": strategy,
        "strategy_parse_error": strategy_parse_error,
        "program_rules_applied": True,
        "program_rule_version": PANEL_RULES["version"],
    }


def merge_local_outputs_into_dify_result(dify_result: Dict[str, Any], local_outputs: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(dify_result, dict):
        dify_result = {}
    if "data" not in dify_result or not isinstance(dify_result.get("data"), dict):
        dify_result["data"] = {}
    if "outputs" not in dify_result["data"] or not isinstance(dify_result["data"].get("outputs"), dict):
        dify_result["data"]["outputs"] = {}

    outputs = dify_result["data"]["outputs"]
    outputs["report_text"] = outputs.get("report_text") or local_outputs["report_text"]
    outputs["panel_dxf"] = outputs.get("panel_dxf") or local_outputs["panel_dxf"]
    outputs["panel_dxf_info"] = outputs.get("panel_dxf_info") or local_outputs["panel_dxf_info"]
    outputs["fallback_output_object_key"] = local_outputs["output_object_key"]
    outputs["fallback_output_filename"] = local_outputs["output_filename"]
    outputs["fallback_download_url"] = local_outputs["download_url"]
    outputs["geometry_mode"] = local_outputs["geometry_mode"]
    outputs["geometry_info"] = local_outputs["geometry_info"]
    outputs["display_candidates"] = local_outputs["display_candidates"]
    outputs["strategy_applied"] = local_outputs["strategy_applied"]
    outputs["strategy_source"] = local_outputs["strategy_source"]
    outputs["strategy_json"] = local_outputs["strategy_json"]
    outputs["strategy_parse_error"] = local_outputs["strategy_parse_error"]
    outputs["program_rules_applied"] = local_outputs["program_rules_applied"]
    outputs["program_rule_version"] = local_outputs["program_rule_version"]
    dify_result["data"]["outputs"] = outputs
    return dify_result


# =========================================================
# API：上傳 DXF
# =========================================================

@app.post("/api/pcb/upload-dxf-to-minio")
async def upload_dxf_to_minio(dxf_file: UploadFile = File(...)):
    if not dxf_file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    if not dxf_file.filename.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="Only .dxf file is allowed")

    file_id = str(uuid.uuid4())
    safe_filename = safe_filename_name(dxf_file.filename)
    object_key = f"uploads/{file_id}/{safe_filename}"
    tmp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{safe_filename}")

    with open(tmp_path, "wb") as buffer:
        shutil.copyfileobj(dxf_file.file, buffer)

    file_size = os.path.getsize(tmp_path)
    upload_file_to_minio(local_path=tmp_path, object_name=object_key, content_type="application/dxf")

    try:
        os.remove(tmp_path)
    except Exception:
        pass

    return {
        "status": "success",
        "file_id": file_id,
        "object_key": object_key,
        "filename": safe_filename,
        "file_size_bytes": file_size,
        "message": "DXF uploaded to MinIO successfully. Use object_key in Dify workflow.",
    }


# =========================================================
# API：候選方案
# =========================================================

@app.post("/api/pcb/generate-panel-candidates-from-minio")
async def generate_panel_candidates_from_minio(
    object_key: str = Form(...),
    product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...),
    single_board_width: float = Form(...),
    rail_width: float = Form(PANEL_RULES["rail"]["default_mm"]),
    smt_max_length: float = Form(330.0),
    smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0),
    ict_max_width: float = Form(300.0),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No"),
):
    local_input = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_input.dxf")
    download_file_from_minio(object_key, local_input)

    detected_length = single_board_length
    detected_width = single_board_width

    try:
        source_doc = ezdxf.readfile(local_input)
        detected_bbox = get_modelspace_bbox(source_doc)
        if detected_bbox:
            min_x, min_y, max_x, max_y = detected_bbox
            detected_length = max_x - min_x
            detected_width = max_y - min_y
    except Exception:
        pass

    try:
        os.remove(local_input)
    except Exception:
        pass

    result = calculate_candidates(
        detected_length,
        detected_width,
        rail_width,
        smt_max_length,
        smt_max_width,
        ict_max_length,
        ict_max_width,
        yes_no_to_bool(has_bga_qfn),
        yes_no_to_bool(has_dip),
        yes_no_to_bool(has_heavy_component),
        yes_no_to_bool(is_irregular_shape),
    )

    report_text = build_ai_report_markdown(product_name=product_name, object_key=object_key, result=result)

    return {
        "product_name": product_name,
        "object_key": object_key,
        "stage": "program_rule_base_panelization",
        "note": "已讀取 DXF bounding box，並依程式內建公司連板規範產生候選方案。",
        "detected_single_board": {"length_mm": round(detected_length, 3), "width_mm": round(detected_width, 3)},
        "input_single_board": {"length_mm": single_board_length, "width_mm": single_board_width},
        "machine_limits": {
            "smt_max_length": smt_max_length,
            "smt_max_width": smt_max_width,
            "ict_max_length": ict_max_length,
            "ict_max_width": ict_max_width,
        },
        "rule_source": PANEL_RULES["source"],
        "rule_version": PANEL_RULES["version"],
        "best_candidate": result["best_candidate"],
        "display_candidates": result["display_candidates"],
        "all_candidates": result["all_candidates"],
        "candidates": result["candidates"],
        "recommended_candidates": result["recommended_candidates"],
        "caution_candidates": result["caution_candidates"],
        "not_recommended_candidates": result["not_recommended_candidates"],
        "comparison_table_markdown": build_comparison_table_markdown(result["all_candidates"], limit=3),
        "caution_and_not_recommended_summary": build_caution_and_not_summary(result["all_candidates"], limit=3),
        "not_recommended_summary": build_caution_and_not_summary(result["all_candidates"], limit=3),
        "report_text": report_text,
        "report_markdown": report_text,
    }


# =========================================================
# API：產生 DXF
# =========================================================

@app.post("/api/pcb/generate-panel-dxf-from-minio")
async def generate_panel_dxf_from_minio(
    object_key: str = Form(...),
    product_name: str = Form("UNKNOWN"),
    single_board_length: float = Form(...),
    single_board_width: float = Form(...),
    rail_width: float = Form(PANEL_RULES["rail"]["default_mm"]),
    smt_max_length: float = Form(330.0),
    smt_max_width: float = Form(250.0),
    ict_max_length: float = Form(350.0),
    ict_max_width: float = Form(300.0),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No"),
    strategy_json: str = Form(""),
):
    inputs = {
        "object_key": object_key,
        "product_name": product_name,
        "single_board_length": str(single_board_length),
        "single_board_width": str(single_board_width),
        "rail_width": str(rail_width),
        "smt_max_length": str(smt_max_length),
        "smt_max_width": str(smt_max_width),
        "ict_max_length": str(ict_max_length),
        "ict_max_width": str(ict_max_width),
        "has_bga_qfn": has_bga_qfn,
        "has_dip": has_dip,
        "has_heavy_component": has_heavy_component,
        "is_irregular_shape": is_irregular_shape,
    }
    outputs = generate_local_panelization_outputs(inputs, strategy_json=strategy_json)
    return outputs["panel_dxf"]


# =========================================================
# API：下載 DXF
# =========================================================

@app.get("/api/pcb/download")
def download_from_minio(object_key: str):
    local_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{os.path.basename(object_key)}")
    download_file_from_minio(object_key, local_path)
    return FileResponse(local_path, media_type="application/dxf", filename=os.path.basename(object_key))


# =========================================================
# 背景任務
# =========================================================

def run_dify_job_background(job_id: str, inputs: Dict[str, str]):
    JOB_STORE[job_id]["status"] = "running"
    JOB_STORE[job_id]["message"] = "依公司連板規範產生 ME 模板連版 DXF 並執行 Dify Workflow..."
    JOB_STORE[job_id]["started_at"] = datetime.utcnow().isoformat()

    try:
        local_outputs = generate_local_panelization_outputs(inputs, strategy_json="")

        if not DIFY_API_BASE or not DIFY_API_KEY:
            fallback_result = {
                "task_id": None,
                "workflow_run_id": None,
                "data": {
                    "status": "program_rule_success",
                    "outputs": {
                        "report_text": local_outputs["report_text"],
                        "panel_dxf": local_outputs["panel_dxf"],
                        "panel_dxf_info": local_outputs["panel_dxf_info"],
                        "fallback_output_object_key": local_outputs["output_object_key"],
                        "fallback_output_filename": local_outputs["output_filename"],
                        "fallback_download_url": local_outputs["download_url"],
                        "geometry_mode": local_outputs["geometry_mode"],
                        "geometry_info": local_outputs["geometry_info"],
                        "display_candidates": local_outputs["display_candidates"],
                        "strategy_applied": local_outputs["strategy_applied"],
                        "strategy_source": local_outputs["strategy_source"],
                        "strategy_json": local_outputs["strategy_json"],
                        "strategy_parse_error": local_outputs["strategy_parse_error"],
                        "program_rules_applied": local_outputs["program_rules_applied"],
                        "program_rule_version": local_outputs["program_rule_version"],
                    },
                    "warning": "Dify API environment variables are not configured, but program-rule DXF generation succeeded.",
                },
            }
            JOB_STORE[job_id]["status"] = "success"
            JOB_STORE[job_id]["message"] = "本地公司規範 ME 模板連版 DXF 已成功產生"
            JOB_STORE[job_id]["result"] = fallback_result
            JOB_STORE[job_id]["error"] = None
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

        url = f"{DIFY_API_BASE.rstrip('/')}/workflows/run"
        payload = {
            "inputs": {
                "product_name": str(inputs.get("product_name", "")),
                "object_key": str(inputs.get("object_key", "")),
                "single_board_length": str(inputs.get("single_board_length", "")),
                "single_board_width": str(inputs.get("single_board_width", "")),
                "rail_width": str(inputs.get("rail_width", str(PANEL_RULES["rail"]["default_mm"]))),
                "smt_max_length": str(inputs.get("smt_max_length", "330")),
                "smt_max_width": str(inputs.get("smt_max_width", "250")),
                "ict_max_length": str(inputs.get("ict_max_length", "350")),
                "ict_max_width": str(inputs.get("ict_max_width", "300")),
                "has_bga_qfn": normalize_yes_no(inputs.get("has_bga_qfn", "No")),
                "has_dip": normalize_yes_no(inputs.get("has_dip", "No")),
                "has_heavy_component": normalize_yes_no(inputs.get("has_heavy_component", "No")),
                "is_irregular_shape": normalize_yes_no(inputs.get("is_irregular_shape", "No")),
            },
            "response_mode": "blocking",
            "user": "pcb-upload-page",
        }
        headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
        response = requests.post(url, json=payload, headers=headers, timeout=600)

        if response.status_code >= 400:
            fallback_result = {
                "task_id": None,
                "workflow_run_id": None,
                "data": {
                    "status": "program_rule_success",
                    "outputs": {
                        "report_text": local_outputs["report_text"],
                        "panel_dxf": local_outputs["panel_dxf"],
                        "panel_dxf_info": local_outputs["panel_dxf_info"],
                        "fallback_output_object_key": local_outputs["output_object_key"],
                        "fallback_output_filename": local_outputs["output_filename"],
                        "fallback_download_url": local_outputs["download_url"],
                        "geometry_mode": local_outputs["geometry_mode"],
                        "geometry_info": local_outputs["geometry_info"],
                        "display_candidates": local_outputs["display_candidates"],
                        "strategy_applied": local_outputs["strategy_applied"],
                        "strategy_source": local_outputs["strategy_source"],
                        "strategy_json": local_outputs["strategy_json"],
                        "strategy_parse_error": local_outputs["strategy_parse_error"],
                        "program_rules_applied": local_outputs["program_rules_applied"],
                        "program_rule_version": local_outputs["program_rule_version"],
                    },
                    "warning": "Dify workflow API failed, but program-rule DXF generation succeeded.",
                    "dify_error": response.text,
                },
            }
            JOB_STORE[job_id]["status"] = "success"
            JOB_STORE[job_id]["message"] = "Dify 失敗，但公司規範 ME 模板連版 DXF 已成功產生"
            JOB_STORE[job_id]["result"] = fallback_result
            JOB_STORE[job_id]["error"] = None
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            fallback_result = {
                "task_id": None,
                "workflow_run_id": None,
                "data": {
                    "status": "program_rule_success",
                    "outputs": {
                        "report_text": local_outputs["report_text"],
                        "panel_dxf": local_outputs["panel_dxf"],
                        "panel_dxf_info": local_outputs["panel_dxf_info"],
                        "fallback_output_object_key": local_outputs["output_object_key"],
                        "fallback_output_filename": local_outputs["output_filename"],
                        "fallback_download_url": local_outputs["download_url"],
                        "geometry_mode": local_outputs["geometry_mode"],
                        "geometry_info": local_outputs["geometry_info"],
                        "display_candidates": local_outputs["display_candidates"],
                        "strategy_applied": local_outputs["strategy_applied"],
                        "strategy_source": local_outputs["strategy_source"],
                        "strategy_json": local_outputs["strategy_json"],
                        "strategy_parse_error": local_outputs["strategy_parse_error"],
                        "program_rules_applied": local_outputs["program_rules_applied"],
                        "program_rule_version": local_outputs["program_rule_version"],
                    },
                    "warning": "Dify returned non-JSON response, but program-rule DXF generation succeeded.",
                    "dify_response_preview": response.text[:1500],
                },
            }
            JOB_STORE[job_id]["status"] = "success"
            JOB_STORE[job_id]["message"] = "Dify 回傳非 JSON，但公司規範 ME 模板連版 DXF 已成功產生"
            JOB_STORE[job_id]["result"] = fallback_result
            JOB_STORE[job_id]["error"] = None
            JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()
            return

        result_json = response.json()
        dify_data = result_json.get("data", {})
        dify_status = dify_data.get("status", "")
        result_json = merge_local_outputs_into_dify_result(result_json, local_outputs)

        if dify_status == "failed":
            result_json["data"]["status"] = "program_rule_success"
            result_json["data"]["warning"] = "Dify workflow failed, but program-rule DXF generation succeeded."
            result_json["data"]["original_dify_status"] = "failed"

        JOB_STORE[job_id]["status"] = "success"
        JOB_STORE[job_id]["message"] = "公司規範 ME 模板連版 DXF 已完成，Dify 結果已合併"
        JOB_STORE[job_id]["result"] = result_json
        JOB_STORE[job_id]["error"] = None
        JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()

    except Exception as e:
        JOB_STORE[job_id]["status"] = "failed"
        JOB_STORE[job_id]["message"] = "公司規範 ME 模板連版 DXF 產生失敗"
        JOB_STORE[job_id]["error"] = {"error_type": type(e).__name__, "error_detail": str(e), "traceback": traceback.format_exc()}
        JOB_STORE[job_id]["finished_at"] = datetime.utcnow().isoformat()


@app.post("/api/pcb/start-dify-job")
async def start_dify_job(
    background_tasks: BackgroundTasks,
    object_key: str = Form(...),
    product_name: str = Form(...),
    single_board_length: str = Form(...),
    single_board_width: str = Form(...),
    rail_width: str = Form(str(PANEL_RULES["rail"]["default_mm"])),
    smt_max_length: str = Form("330"),
    smt_max_width: str = Form("250"),
    ict_max_length: str = Form("350"),
    ict_max_width: str = Form("300"),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No"),
):
    job_id = str(uuid.uuid4())
    inputs = {
        "object_key": object_key,
        "product_name": product_name,
        "single_board_length": single_board_length,
        "single_board_width": single_board_width,
        "rail_width": rail_width,
        "smt_max_length": smt_max_length,
        "smt_max_width": smt_max_width,
        "ict_max_length": ict_max_length,
        "ict_max_width": ict_max_width,
        "has_bga_qfn": has_bga_qfn,
        "has_dip": has_dip,
        "has_heavy_component": has_heavy_component,
        "is_irregular_shape": is_irregular_shape,
    }
    JOB_STORE[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "message": "任務已建立，等待背景執行",
        "inputs": inputs,
        "created_at": datetime.utcnow().isoformat(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    background_tasks.add_task(run_dify_job_background, job_id, inputs)
    return {"status": "accepted", "job_id": job_id, "message": "Dify Workflow background job started."}


@app.get("/api/pcb/job-status/{job_id}")
def get_job_status(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")
    return job


@app.post("/api/pcb/run-dify-panelization")
async def run_dify_panelization(
    object_key: str = Form(...),
    product_name: str = Form(...),
    single_board_length: str = Form(...),
    single_board_width: str = Form(...),
    rail_width: str = Form(str(PANEL_RULES["rail"]["default_mm"])),
    smt_max_length: str = Form("330"),
    smt_max_width: str = Form("250"),
    ict_max_length: str = Form("350"),
    ict_max_width: str = Form("300"),
    has_bga_qfn: str = Form("No"),
    has_dip: str = Form("No"),
    has_heavy_component: str = Form("No"),
    is_irregular_shape: str = Form("No"),
    strategy_json: str = Form(""),
):
    inputs = {
        "object_key": object_key,
        "product_name": product_name,
        "single_board_length": single_board_length,
        "single_board_width": single_board_width,
        "rail_width": rail_width,
        "smt_max_length": smt_max_length,
        "smt_max_width": smt_max_width,
        "ict_max_length": ict_max_length,
        "ict_max_width": ict_max_width,
        "has_bga_qfn": has_bga_qfn,
        "has_dip": has_dip,
        "has_heavy_component": has_heavy_component,
        "is_irregular_shape": is_irregular_shape,
    }
    local_outputs = generate_local_panelization_outputs(inputs, strategy_json=strategy_json)
    return {
        "status": "success",
        "message": "Synchronous API used real DXF panelization output with embedded company panel rules and detailed ME template layout.",
        "data": {
            "outputs": {
                "report_text": local_outputs["report_text"],
                "panel_dxf": local_outputs["panel_dxf"],
                "panel_dxf_info": local_outputs["panel_dxf_info"],
                "fallback_output_object_key": local_outputs["output_object_key"],
                "fallback_output_filename": local_outputs["output_filename"],
                "fallback_download_url": local_outputs["download_url"],
                "geometry_mode": local_outputs["geometry_mode"],
                "geometry_info": local_outputs["geometry_info"],
                "display_candidates": local_outputs["display_candidates"],
                "strategy_applied": local_outputs["strategy_applied"],
                "strategy_source": local_outputs["strategy_source"],
                "strategy_json": local_outputs["strategy_json"],
                "strategy_parse_error": local_outputs["strategy_parse_error"],
                "program_rules_applied": local_outputs["program_rules_applied"],
                "program_rule_version": local_outputs["program_rule_version"],
            }
        },
    }


# =========================================================
# Index.html 相容 API：/api/analyze
# 目的：
# 1. 保留目前 index.html 畫面與欄位名稱。
# 2. 後端仍以 main(1).py 的公司規範、候選方案、Dify / MinIO 架構為主體。
# 3. 產出的 DXF 走 create_real_panel_dxf_from_source()，會複製原始 DXF modelspace 圖元，不是模擬矩形。
# =========================================================

def _read_dxf_doc_stable_from_bytes(content: bytes):
    tmp_path = None
    try:
        return ezdxf.read(io.StringIO(content.decode("utf-8", errors="ignore")))
    except Exception:
        pass

    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False, mode="wb") as f:
            f.write(content)
            tmp_path = f.name
        return ezdxf.readfile(tmp_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _collect_preview_entities(doc):
    xs, ys, items = [], [], []
    for e in doc.modelspace():
        t = e.dxftype()
        try:
            if t == "LINE":
                x1, y1 = float(e.dxf.start.x), float(e.dxf.start.y)
                x2, y2 = float(e.dxf.end.x), float(e.dxf.end.y)
                xs += [x1, x2]
                ys += [y1, y2]
                items.append(("L", x1, y1, x2, y2))

            elif t == "LWPOLYLINE":
                pts = list(e.get_points())
                for i in range(len(pts) - 1):
                    x1, y1 = float(pts[i][0]), float(pts[i][1])
                    x2, y2 = float(pts[i + 1][0]), float(pts[i + 1][1])
                    xs += [x1, x2]
                    ys += [y1, y2]
                    items.append(("L", x1, y1, x2, y2))
                if e.is_closed and len(pts) > 1:
                    x1, y1 = float(pts[-1][0]), float(pts[-1][1])
                    x2, y2 = float(pts[0][0]), float(pts[0][1])
                    xs += [x1, x2]
                    ys += [y1, y2]
                    items.append(("L", x1, y1, x2, y2))

            elif t == "POLYLINE":
                pts = []
                for v in e.vertices:
                    x, y = float(v.dxf.location.x), float(v.dxf.location.y)
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
                cx, cy, r = float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius)
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
                items.append(("C", cx, cy, r))

            elif t == "ARC":
                cx, cy, r = float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius)
                sa, ea = float(e.dxf.start_angle), float(e.dxf.end_angle)
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
                items.append(("A", cx, cy, r, sa, ea))

            elif t == "SPLINE":
                try:
                    for cp in e.control_points:
                        xs.append(float(cp[0]))
                        ys.append(float(cp[1]))
                except Exception:
                    pass

        except Exception:
            continue

    return xs, ys, items


def dxf_to_svg_for_index(content: bytes) -> str:
    """
    原始 PCB 外形預覽。
    只做前端預覽用，不影響實際 DXF 連版輸出。
    """
    try:
        doc = _read_dxf_doc_stable_from_bytes(content)
    except Exception:
        return ""

    xs, ys, items = _collect_preview_entities(doc)
    if not xs or not ys or not items:
        return ""

    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return ""

    vw, vh, margin = 520, 260, 18
    scale = min((vw - margin * 2) / w, (vh - margin * 2) / h)
    ox = margin + (vw - margin * 2 - w * scale) / 2
    oy = margin + (vh - margin * 2 - h * scale) / 2

    def tx(x):
        return round(ox + (x - minx) * scale, 2)

    def ty(y):
        return round(oy + (maxy - y) * scale, 2)

    els = [f'<rect x="0" y="0" width="{vw}" height="{vh}" fill="#0b0f18"/>']

    for it in items:
        if it[0] == "L":
            _, x1, y1, x2, y2 = it
            els.append(
                f'<line x1="{tx(x1)}" y1="{ty(y1)}" x2="{tx(x2)}" y2="{ty(y2)}" '
                f'stroke="#f7e600" stroke-width="1.2" fill="none"/>'
            )
        elif it[0] == "C":
            _, cx, cy, r = it
            els.append(
                f'<circle cx="{tx(cx)}" cy="{ty(cy)}" r="{round(r * scale, 2)}" '
                f'stroke="#ff66cc" stroke-width="1.1" fill="none"/>'
            )
        elif it[0] == "A":
            _, cx, cy, r, sa, ea = it
            x1 = cx + r * math.cos(math.radians(sa))
            y1 = cy + r * math.sin(math.radians(sa))
            x2 = cx + r * math.cos(math.radians(ea))
            y2 = cy + r * math.sin(math.radians(ea))
            large_arc = 1 if (ea - sa) % 360 > 180 else 0
            els.append(
                f'<path d="M{tx(x1)} {ty(y1)} A{round(r * scale, 2)} {round(r * scale, 2)} '
                f'0 {large_arc} 0 {tx(x2)} {ty(y2)}" stroke="#ff66cc" stroke-width="1.1" fill="none"/>'
            )

    els.append(
        f'<rect x="{margin / 2}" y="{margin / 2}" width="{vw - margin}" height="{vh - margin}" '
        f'stroke="#334155" stroke-width="1" fill="none" rx="4"/>'
    )

    return (
        f'<svg viewBox="0 0 {vw} {vh}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:100%;display:block">{"".join(els)}</svg>'
    )


def panel_svg_for_index(best_candidate: Dict[str, Any]) -> str:
    """
    前端右側 AI 建議連版圖預覽。
    實際下載 DXF 仍以真實原始 DXF 複製輸出為準。
    """
    cols = int(best_candidate.get("columns", best_candidate.get("x_count", 1)))
    rows = int(best_candidate.get("rows", best_candidate.get("y_count", 1)))
    board_w = float(best_candidate.get("_detected_board_w", 20.0))
    board_h = float(best_candidate.get("_detected_board_h", 14.0))
    gap_x = float(best_candidate.get("gap_x_mm", 2.0))
    gap_y = float(best_candidate.get("gap_y_mm", 2.0))
    rail_left = float(best_candidate.get("rail_left_mm", 0.0))
    rail_right = float(best_candidate.get("rail_right_mm", 0.0))
    rail_top = float(best_candidate.get("rail_top_mm", 0.0))
    rail_bottom = float(best_candidate.get("rail_bottom_mm", 0.0))
    panel_w = float(best_candidate.get("panel_length_mm", 0.0))
    panel_h = float(best_candidate.get("panel_width_mm", 0.0))

    if panel_w <= 0:
        panel_w = rail_left + rail_right + cols * board_w + max(cols - 1, 0) * gap_x
    if panel_h <= 0:
        panel_h = rail_top + rail_bottom + rows * board_h + max(rows - 1, 0) * gap_y

    vw, vh, margin = 340, 340, 30
    scale = min((vw - margin * 2) / max(panel_w, 1), (vh - margin * 2) / max(panel_h, 1))
    ox = margin + (vw - margin * 2 - panel_w * scale) / 2
    oy = margin + (vh - margin * 2 - panel_h * scale) / 2

    def tx(x):
        return round(ox + x * scale, 2)

    def ty(y):
        return round(oy + (panel_h - y) * scale, 2)

    els = []
    els.append(
        f'<rect x="{tx(0)}" y="{ty(panel_h)}" width="{round(panel_w * scale, 2)}" '
        f'height="{round(panel_h * scale, 2)}" fill="#0d1f0d" stroke="#38c6a0" stroke-width="2" rx="3"/>'
    )

    if rail_left > 0:
        els.append(f'<rect x="{tx(0)}" y="{ty(panel_h)}" width="{round(rail_left*scale,2)}" height="{round(panel_h*scale,2)}" fill="rgba(56,198,160,0.15)"/>')
    if rail_right > 0:
        els.append(f'<rect x="{tx(panel_w-rail_right)}" y="{ty(panel_h)}" width="{round(rail_right*scale,2)}" height="{round(panel_h*scale,2)}" fill="rgba(56,198,160,0.15)"/>')
    if rail_bottom > 0:
        els.append(f'<rect x="{tx(0)}" y="{ty(rail_bottom)}" width="{round(panel_w*scale,2)}" height="{round(rail_bottom*scale,2)}" fill="rgba(56,198,160,0.15)"/>')
    if rail_top > 0:
        els.append(f'<rect x="{tx(0)}" y="{ty(panel_h)}" width="{round(panel_w*scale,2)}" height="{round(rail_top*scale,2)}" fill="rgba(56,198,160,0.15)"/>')

    for c in range(cols):
        for r in range(rows):
            x = rail_left + c * (board_w + gap_x)
            y = rail_bottom + r * (board_h + gap_y)
            els.append(
                f'<rect x="{tx(x)}" y="{ty(y+board_h)}" width="{round(board_w*scale,2)}" height="{round(board_h*scale,2)}" '
                f'fill="rgba(79,142,247,0.18)" stroke="#4f8ef7" stroke-width="1.2" rx="1"/>'
            )

    if "V" in str(best_candidate.get("split_method", "")):
        for c in range(1, cols):
            vx = rail_left + c * board_w + (c - 0.5) * gap_x
            els.append(f'<line x1="{tx(vx)}" y1="{ty(panel_h)}" x2="{tx(vx)}" y2="{ty(0)}" stroke="#f0a732" stroke-width="1.2" stroke-dasharray="5,3"/>')
        for r in range(1, rows):
            vy = rail_bottom + r * board_h + (r - 0.5) * gap_y
            els.append(f'<line x1="{tx(0)}" y1="{ty(vy)}" x2="{tx(panel_w)}" y2="{ty(vy)}" stroke="#f0a732" stroke-width="1.2" stroke-dasharray="5,3"/>')

    for mx, my in [(5, panel_h-5), (panel_w-5, panel_h-5), (5, 5)]:
        els.append(f'<circle cx="{tx(mx)}" cy="{ty(my)}" r="3" fill="#e05c5c"/>')

    els.append(
        f'<text x="{tx(panel_w/2)}" y="{ty(0)+18}" text-anchor="middle" font-size="10" fill="#4f8ef7" font-family="Arial">'
        f'{cols}x{rows}={cols*rows}pcs</text>'
    )

    return f'<svg viewBox="0 0 {vw} {vh}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">{"".join(els)}</svg>'


@app.post("/api/analyze")
async def analyze_for_current_index(
    file: UploadFile = File(...),
    product_name: str = Form("PCB-001"),
    pcb_length: float = Form(0),
    pcb_width: float = Form(0),
    thickness: float = Form(1.6),
    rail_mode: str = Form("AI自動"),
    process_type: str = Form("SMT"),
    has_bga: str = Form("N"),
    is_irregular: str = Form("N"),
    has_finger: str = Form("N"),
    has_tall: str = Form("N"),
    max_panel_length: float = Form(350),
    max_panel_width: float = Form(260),
):
    """
    給目前 index.html 使用的 API。

    重要：
    - 前端仍維持目前畫面與 /api/analyze。
    - 後端實際使用 main(1).py 的公司連板規範與候選邏輯。
    - 輸出的 DXF 透過 create_real_panel_dxf_from_source() 實際複製原始 DXF 圖元。
    """
    if not file or not file.filename or not file.filename.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="請上傳 .dxf 檔案，系統會依原始 DXF 實際產生連版圖。")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="DXF 檔案是空的。")

    orig_svg = dxf_to_svg_for_index(content)

    safe_name = safe_filename_name(file.filename)
    file_id = str(uuid.uuid4())
    object_key = f"uploads/{file_id}/{safe_name}"
    tmp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{safe_name}")

    with open(tmp_path, "wb") as f:
        f.write(content)

    upload_file_to_minio(local_path=tmp_path, object_name=object_key, content_type="application/dxf")

    has_bga_qfn = "Yes" if str(has_bga).upper() == "Y" else "No"
    is_irregular_shape = "Yes" if str(is_irregular).upper() == "Y" else "No"
    has_heavy_component = "Yes" if str(has_tall).upper() == "Y" else "No"
    has_dip = "Yes" if ("DIP" in str(process_type).upper() or "THT" in str(process_type).upper()) else "No"

    smt_max_length = max_panel_length
    smt_max_width = max_panel_width
    ict_max_length = max_panel_length
    ict_max_width = max_panel_width

    inputs = {
        "object_key": object_key,
        "product_name": product_name,
        "single_board_length": str(pcb_length),
        "single_board_width": str(pcb_width),
        "rail_width": str(PANEL_RULES["rail"]["default_mm"]),
        "smt_max_length": str(smt_max_length),
        "smt_max_width": str(smt_max_width),
        "ict_max_length": str(ict_max_length),
        "ict_max_width": str(ict_max_width),
        "has_bga_qfn": has_bga_qfn,
        "has_dip": has_dip,
        "has_heavy_component": has_heavy_component,
        "is_irregular_shape": is_irregular_shape,
    }

    try:
        outputs = generate_local_panelization_outputs(inputs, strategy_json="")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    best = dict(outputs["best_candidate"])
    geom = outputs.get("geometry_info", {}) or {}
    detected_w = float(geom.get("detected_board_width_mm", pcb_length or 0) or 0)
    detected_h = float(geom.get("detected_board_height_mm", pcb_width or 0) or 0)

    panel = {
        "nx": int(best.get("columns", best.get("x_count", 1))),
        "ny": int(best.get("rows", best.get("y_count", 1))),
        "pcb_count": int(best.get("pcs_per_panel", 1)),
        "total_length": float(best.get("panel_length_mm", 0)),
        "total_width": float(best.get("panel_width_mm", 0)),
        "top_rail": float(best.get("rail_top_mm", 0)),
        "bot_rail": float(best.get("rail_bottom_mm", 0)),
        "left_rail": float(best.get("rail_left_mm", 0)),
        "right_rail": float(best.get("rail_right_mm", 0)),
        "gap": float(best.get("gap_x_mm", PANEL_RULES["cutting"]["router_min_gap_mm"])),
        "vcut_ok": "V" in str(best.get("split_method", "")),
        "vcut_angle": PANEL_RULES["cutting"]["vcut_angle_deg"],
        "vcut_residual": 0.4,
        "mark_diameter": PANEL_RULES["fiducial"]["diameter_mm"],
        "split_method": best.get("split_method", ""),
        "rail_policy": best.get("rail_policy", ""),
    }

    best["_detected_board_w"] = detected_w
    best["_detected_board_h"] = detected_h

    report = outputs.get("report_text", "")
    if not report:
        report = "已完成連版規劃。"

    return JSONResponse({
        "success": True,
        "auto_detected": True,
        "pcb_length": round(detected_w, 3),
        "pcb_width": round(detected_h, 3),
        "panel": panel,
        "download_url": outputs.get("api_download_url") or outputs.get("download_url"),
        "minio_presigned_url": outputs.get("minio_presigned_url"),
        "ai_report": report,
        "orig_svg": orig_svg,
        "panel_svg": panel_svg_for_index(best),
        "real_dxf_panelization": True,
        "geometry_mode": outputs.get("geometry_mode"),
        "output_object_key": outputs.get("output_object_key"),
    })
