#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ECMWF 海浪模式（WAM）预报图 —— 华南近海 + 南海北部

产品：
  - swh: 有效波高（填色）+ 平均浪向（箭头）

数据来源：ECMWF Open Data 的 IFS 海浪模式（stream=wave，0.25°，GRIB2），
  https://data.ecmwf.int/forecasts/{yyyymmdd}/{hh}z/ifs/0p25/wave/
    {yyyymmddHHMMSS}-{step}h-wave-fc.index   每个变量的字节偏移
    {yyyymmddHHMMSS}-{step}h-wave-fc.grib2   该时次全部海浪变量

做法与 aifs-prediction/aifs_chart.py 一致：
  1. 先取 .index，只按字节区间（HTTP Range）下载 swh / mwd / mwp，
     一步约 1.3 MB，39 步合计约 52 MB；
  2. eccodes 解码，裁出公共窗口，陆地缺测（9999）转 NaN；
  3. 每步缓存成 _cache/steps/*.npz，重跑不重复下载；
  4. 按 AIFS 的版式出图：10×7.2、白底、左上标题、右上有效时间、
     右侧竖色标、右下 www.bamc.top、广东省界黄色描边。

用法：
  python ec_wave_chart.py                          # 自动选最新完整起报场，出 39 帧 + 总览
  python ec_wave_chart.py --run 2026-11-17T00:00   # 指定起报场
  python ec_wave_chart.py --steps 24,48,72         # 只出部分时次
  python ec_wave_chart.py --domain scs             # 换海域（wave / scs / ea）
  python ec_wave_chart.py --input _cache/steps --run ...   # 用已下载缓存重画

依赖：numpy matplotlib cartopy eccodes
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
LIVE_DIR = BASE_DIR / "wave"
OUT_DIR = LIVE_DIR
CACHE_DIR = BASE_DIR / "_cache" / "steps"
TZ_CN = timezone(timedelta(hours=8))
UTC = timezone.utc
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/126 Safari/537.36")

# 这台机器访问 ECMWF 时 TLS 被中间代理改写，证书链验证不过；Open Data 只读，
# 默认放宽校验。需要严格校验时设 EC_WAVE_VERIFY_SSL=1。
VERIFY_SSL = os.environ.get("EC_WAVE_VERIFY_SSL", "0") == "1"

OPEN_DATA_BASE = os.environ.get(
    "EC_WAVE_OPEN_DATA_BASE", "https://data.ecmwf.int/forecasts")
MODEL = "ifs"
RESOL = "0p25"
STREAM = "wave"

DOMAINS = {
    # 默认：华南近海 + 南海北部（与 AIFS 华南图的纬度带衔接，向东带到台湾以东）
    "wave": {"west": 105.0, "south": 15.0, "east": 122.0, "north": 28.0},
    # 整个南海 + 西北太平洋的一部分
    "scs": {"west": 105.0, "south": 5.0, "east": 135.0, "north": 30.0},
    # 东亚大范围
    "ea": {"west": 80.0, "south": 0.0, "east": 140.0, "north": 50.0},
}
DEFAULT_DOMAIN = "wave"

# 下载窗口比作图窗口略大，换 domain 时不用重新下载。
BOX = {"west": 104.5, "south": 4.5, "east": 135.5, "north": 30.5}

LEADS = list(range(12, 241, 6))       # +12h … +240h，每 6 小时一帧，共 39 帧
OVERVIEW_STEPS = list(range(12, 241, 12))   # 总览：每 12 小时一个，共 20 个小图
RUN_HOURS = (0, 12)                   # 与 AIFS 站点一致，只用 00Z / 12Z
COMPLETE_STEP = 240                   # 有这个时次才算完整场次
PARAMS = ["swh", "mwd", "mwp"]        # mwp 只缓存，暂不单独出图

MISSING = 9999.0

# 有效波高色阶（m）：白 → 蓝 → 青绿 → 黄 → 橙红 → 紫。
# 0–12 m 覆盖南海常见浪高到台风极值，>12 m 用紫色收尾。
SWH_LEVELS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0,
              5.0, 6.0, 7.0, 8.0, 10.0, 12.0]
SWH_LABELS = ["0", "0.5", "1", "1.5", "2", "2.5", "3", "4",
              "5", "6", "7", "8", "10", "12"]
SWH_COLORS = [
    "#eaf4fb", "#d6ecff", "#a9d4ff", "#74b3f0",
    "#3f8bd8", "#1465b8", "#00a6b8", "#00bf7a",
    "#8fd93b", "#ffe14d", "#ffb31a", "#ff6a00",
    "#e00000", "#b0005a",
]
SWH_OVER = "#6b003f"

# 等值线只画到 10 m，避免台风核心区一堆线。
SWH_CONTOURS = [1, 2, 3, 4, 5, 6, 8, 10]

# 箭头取样：0.25° 网格按海域取不同间隔，保证箭头疏密与 AIFS 风羽接近。
ARROW_SAMPLE = {"wave": 4, "scs": 8, "ea": 12}   # 1° / 2° / 3°
# 浪向箭头长度（英寸）；方向向量取单位向量。
ARROW_SCALE = 5.2

SMOOTH_SIGMA = float(os.environ.get("EC_WAVE_SMOOTH", "0.6"))

# 产品注册表：字段与 server.py / auto_update.py 共用。
PRODUCTS = {
    "swh": {
        "subdir": "swh",
        "prefix": "ec_wave_swh_",
        "title": "Significant Wave Height & Direction",
        "kind": "swh",
        "note": "",
        "colorbar": "significant wave height (m)",
    },
    "overview": {
        "subdir": "overview",
        "prefix": "ec_wave_overview_",
        "title": "20-frame Overview",
        "kind": "overview",
        "frames": 1,
        "note": "",
    },
}
ALL_PRODUCTS = list(PRODUCTS)


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- HTTP
_SSL_LOCK = threading.Lock()
_SSL_CTX: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CTX
    with _SSL_LOCK:
        if _SSL_CTX is None:
            if VERIFY_SSL:
                import certifi

                _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
            else:
                _SSL_CTX = ssl._create_unverified_context()
        return _SSL_CTX


def http_get(url: str, rng: list[tuple[int, int]] | None = None,
             timeout: int = 180, retries: int = 4) -> bytes:
    """GET，可带多段 Range；带退避重试。"""
    last: Exception | None = None
    for attempt in range(retries):
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if rng:
            headers["Range"] = "bytes=" + ",".join(
                "%d-%d" % (a, b) for a, b in rng)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(
                    req, timeout=timeout, context=_ssl_context()) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403, 404):
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(min(30, 3 * (attempt + 1)))
    raise RuntimeError("下载失败（%d 次）：%s：%s" % (retries, url, last))


def http_exists(url: str, timeout: int = 40) -> bool:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA}, method="HEAD")
    try:
        with urllib.request.urlopen(
                req, timeout=timeout, context=_ssl_context()) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 405:  # 有些对象存储不支持 HEAD
            try:
                http_get(url, timeout=timeout, retries=1)
                return True
            except Exception:  # noqa: BLE001
                return False
        return False
    except Exception:  # noqa: BLE001
        return False


def _run_prefix(run_dt: datetime) -> str:
    return "%s/%sz/%s/%s/%s" % (
        run_dt.strftime("%Y%m%d"), run_dt.strftime("%H"),
        MODEL, RESOL, STREAM,
    )


def _step_base_url(run_dt: datetime, step: int) -> str:
    stamp = run_dt.strftime("%Y%m%d%H%M%S")
    return "%s/%s/%s-%dh-%s-fc" % (
        OPEN_DATA_BASE, _run_prefix(run_dt), stamp, step, STREAM,
    )


def fetch_index(run_dt: datetime, step: int) -> list[dict]:
    raw = http_get(_step_base_url(run_dt, step) + ".index", timeout=90)
    records = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def run_state(run_dt: datetime) -> str:
    """missing（还没出）/ partial（还没到 +240h）/ full（到 +240h）。"""
    if not http_exists(_step_base_url(run_dt, COMPLETE_STEP) + ".index"):
        return "missing"
    if not http_exists(_step_base_url(run_dt, 0) + ".index"):
        return "partial"
    return "full"


def latest_run(now: datetime | None = None) -> str:
    """选最新且已经更新到 +240h 的 00Z / 12Z 起报场。"""
    now = (now or datetime.now(UTC)).replace(
        minute=0, second=0, microsecond=0)
    anchor = now.replace(hour=12) if now.hour >= 12 else now.replace(hour=0)
    fallback = ""
    for offset in range(0, 120, 12):
        candidate = anchor - timedelta(hours=offset)
        if candidate.hour not in RUN_HOURS:
            continue
        label = candidate.strftime("%Y-%m-%dT%H:%M")
        log("检查海浪起报场 %s" % label)
        state = run_state(candidate)
        if state == "full":
            log("使用起报场 %s" % label)
            return label
        if state == "partial":
            log("起报场 %s 尚未更新到 +%dh，跳过" % (label, COMPLETE_STEP))
            if not fallback:
                fallback = label
    if fallback:
        log("没有完整场次，回退到最新可用起报场 %s（尾部时次可能偏短）" % fallback)
        return fallback
    raise RuntimeError("没有找到可用的海浪起报场")


# ---------------------------------------------------------------- GRIB 解包
GRID_LOCK = threading.Lock()
GRIB_LOCK = threading.Lock()
GRID_CACHE: dict = {}
LAST_GRID: dict | None = None


def eccodes_get(gid, key):
    import eccodes

    return eccodes.codes_get(gid, key)


def _grid_geometry(gid) -> dict:
    return {
        "ni": int(eccodes_get(gid, "Ni")),
        "nj": int(eccodes_get(gid, "Nj")),
        "lat1": float(eccodes_get(gid, "latitudeOfFirstGridPointInDegrees")),
        "lat2": float(eccodes_get(gid, "latitudeOfLastGridPointInDegrees")),
        "lon1": float(eccodes_get(gid, "longitudeOfFirstGridPointInDegrees")),
        "lon2": float(eccodes_get(gid, "longitudeOfLastGridPointInDegrees")),
    }


def _grid_info(gid) -> dict:
    """返回裁剪后的经纬度（南→北）和行列范围。"""
    global LAST_GRID
    geo = _grid_geometry(gid)
    key = (geo["ni"], geo["nj"], geo["lat1"], geo["lon1"])
    with GRID_LOCK:
        hit = GRID_CACHE.get(key)
        if hit:
            return hit
    ni, nj = geo["ni"], geo["nj"]
    dlat = (geo["lat1"] - geo["lat2"]) / (nj - 1)
    dlon = (geo["lon2"] - geo["lon1"]) / (ni - 1)
    if dlon < 0:
        dlon += 360.0 / (ni - 1)
    lat_full = geo["lat1"] - dlat * np.arange(nj)
    lon_full = (geo["lon1"] + dlon * np.arange(ni)) % 360.0

    rows = np.where((lat_full >= BOX["south"]) & (lat_full <= BOX["north"]))[0]
    cols = np.where((lon_full >= BOX["west"]) & (lon_full <= BOX["east"]))[0]
    if not len(rows) or not len(cols):
        raise RuntimeError("GRIB 网格与目标区域没有交集")
    r0, r1 = int(rows.min()), int(rows.max()) + 1
    c0, c1 = int(cols.min()), int(cols.max()) + 1
    info = {
        "ni": ni, "nj": nj, "r0": r0, "r1": r1, "c0": c0, "c1": c1,
        "lat": np.ascontiguousarray(lat_full[r0:r1][::-1]),   # 南→北
        "lon": np.ascontiguousarray(lon_full[c0:c1]),
    }
    with GRID_LOCK:
        GRID_CACHE[key] = info
        LAST_GRID = info
    return info


def _extract_messages(blob: bytes) -> list[bytes]:
    """从（可能带 multipart 边界的）响应里切出所有 GRIB2 消息。"""
    messages = []
    pos = 0
    while True:
        start = blob.find(b"GRIB", pos)
        if start < 0:
            break
        if start + 16 > len(blob):
            break
        total = int.from_bytes(blob[start + 8:start + 16], "big")
        if total <= 0 or start + total > len(blob):
            break
        messages.append(blob[start:start + total])
        pos = start + total
    return messages


def _decode_subset(message: bytes) -> tuple[str, np.ndarray]:
    import eccodes

    gid = eccodes.codes_new_from_message(message)
    try:
        short = str(eccodes.codes_get(gid, "shortName"))
        grid = _grid_info(gid)
        values = eccodes.codes_get_values(gid)
    finally:
        eccodes.codes_release(gid)
    field = values.reshape(grid["nj"], grid["ni"])[
        grid["r0"]:grid["r1"], grid["c0"]:grid["c1"]]
    # GRIB 的行序北→南，翻成南→北，和绘图顺序一致。
    field = field[::-1]
    field = np.ascontiguousarray(field, dtype=np.float32)
    field[~np.isfinite(field)] = np.nan
    field[field == MISSING] = np.nan
    field[field > 1.0e6] = np.nan
    return short, field


def _eccodes_warmup() -> None:
    """在主线程里先把 eccodes definitions 加载好，避免并发首次初始化崩溃。"""
    import eccodes

    gid = eccodes.codes_grib_new_from_samples("GRIB2")
    try:
        eccodes.codes_get(gid, "shortName")
    finally:
        eccodes.codes_release(gid)


def fetch_fields(run_dt: datetime, step: int,
                 params: list[str]) -> tuple[dict[str, np.ndarray], dict]:
    """按 .index 的字节区间只下载需要的字段。"""
    records = fetch_index(run_dt, step)
    selected = []
    for rec in records:
        if str(rec.get("levtype") or "") != "sfc":
            continue
        if str(rec.get("param") or "") in params:
            selected.append(rec)
    if not selected:
        raise RuntimeError("海浪 %s +%dh 没有匹配的字段" % (
            run_dt.strftime("%Y-%m-%d %H:%M"), step))
    selected.sort(key=lambda r: int(r["_offset"]))
    ranges = [(int(r["_offset"]), int(r["_offset"]) + int(r["_length"]) - 1)
              for r in selected]
    blob = http_get(_step_base_url(run_dt, step) + ".grib2",
                    rng=ranges, timeout=300)
    fields: dict[str, np.ndarray] = {}
    grid = None
    with GRIB_LOCK:
        for message in _extract_messages(blob):
            key, field = _decode_subset(message)
            fields[key] = field
            if grid is None and LAST_GRID is not None:
                grid = {"lat": LAST_GRID["lat"], "lon": LAST_GRID["lon"]}
    missing = sorted(set(params) - set(fields))
    if missing:
        raise RuntimeError("海浪 +%dh 缺少字段：%s" % (step, missing))
    if grid is None:
        raise RuntimeError("海浪 +%dh 没有解出网格" % step)
    return fields, grid


# ---------------------------------------------------------------- 缓存
def _step_cache_path(run_dt: datetime, step: int) -> Path:
    tag = run_dt.strftime("%Y%m%d%H")
    return CACHE_DIR / ("wave_%s_%03d.npz" % (tag, step))


def _save_step(path: Path, arrays: dict, grid: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        swh=arrays["swh"], mwd=arrays["mwd"], mwp=arrays["mwp"],
        lat=grid["lat"], lon=grid["lon"],
    )


def load_step(path: Path) -> tuple[dict, dict]:
    with np.load(path) as data:
        arrays = {k: data[k] for k in ("swh", "mwd", "mwp")}
        grid = {"lat": data["lat"], "lon": data["lon"]}
    return arrays, grid


def build_dataset(run_dt: datetime, steps: list[int],
                  workers: int = 4, reuse: bool = True) -> dict[int, Path]:
    """下载 / 解码 / 缓存需要的时次。"""
    import eccodes  # noqa: F401  提前报缺依赖

    _eccodes_warmup()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[int, Path] = {}
    todo: list[int] = []
    for step in steps:
        path = _step_cache_path(run_dt, step)
        if reuse and path.exists():
            log("+%dh 使用本地缓存" % step)
            result[step] = path
        else:
            todo.append(step)

    def work(step: int) -> tuple[int, Path | None]:
        try:
            fields, grid = fetch_fields(run_dt, step, PARAMS)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:   # 半截场次：尾部时次还没发布
                log("+%dh 数据尚未发布，跳过" % step)
                return step, None
            raise
        path = _step_cache_path(run_dt, step)
        _save_step(path, fields, grid)
        return step, path

    if todo:
        log("下载 %d 个时次（swh/mwd/mwp，%d 路并发）…" % (len(todo), workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, s): s for s in todo}
            for future in as_completed(futures):
                step, path = future.result()
                if path is None:
                    continue
                log("+%dh 下载解码完成" % step)
                result[step] = path
    return dict(sorted(result.items()))


# ---------------------------------------------------------------- 绘图
def smooth(field: np.ndarray, sigma: float | None = None) -> np.ndarray:
    sigma = SMOOTH_SIGMA if sigma is None else sigma
    data = np.asarray(field, dtype=np.float64)
    if sigma <= 0:
        return data
    mask = ~np.isfinite(data)
    filled = np.where(mask, 0.0, data)
    try:
        from scipy.ndimage import gaussian_filter

        num = gaussian_filter(filled, sigma=sigma, mode="nearest")
        den = gaussian_filter((~mask).astype(np.float64), sigma=sigma,
                              mode="nearest")
        out = np.where(den > 1e-6, num / np.maximum(den, 1e-6), np.nan)
    except Exception:  # noqa: BLE001
        out = data
    out[mask] = np.nan
    return out


_GD_CACHE = None
_GD_LOCK = threading.Lock()


def _guangdong_geometries():
    """广东省界几何只读一次（所有帧共用），避免每帧重读 50m shapefile。"""
    global _GD_CACHE
    with _GD_LOCK:
        if _GD_CACHE is not None:
            return _GD_CACHE
    geoms = []
    try:
        from cartopy.io import shapereader

        provinces = shapereader.natural_earth(
            resolution="50m", category="cultural",
            name="admin_1_states_provinces",
        )
        for record in shapereader.Reader(provinces).records():
            attrs = record.attributes
            labels = [str(attrs.get(key) or "")
                      for key in ("name", "name_en", "name_local",
                                  "iso_3166_2")]
            if any("guangdong" in label.lower() or label == "CN-GD"
                   for label in labels):
                geoms.append(record.geometry)
    except Exception:  # noqa: BLE001
        geoms = []
    with _GD_LOCK:
        _GD_CACHE = geoms
    return geoms


def base_map(ax, ccrs, cfeature, domain: str = DEFAULT_DOMAIN):
    d = DOMAINS[domain]
    ax.set_extent([d["west"], d["east"], d["south"], d["north"]],
                  crs=ccrs.PlateCarree())
    ax.add_feature(cfeature.OCEAN.with_scale("10m"),
                   facecolor="#f4f8fb", edgecolor="none", zorder=0)
    ax.add_feature(cfeature.LAND.with_scale("10m"),
                   facecolor="#f5f0e4", edgecolor="none", zorder=0)
    try:
        ax.add_feature(
            cfeature.LAKES.with_scale("10m"),
            facecolor="#f4f8fb", edgecolor="#7e8b93",
            linewidth=0.25, zorder=1,
        )
    except Exception:
        pass
    ax.add_feature(
        cfeature.BORDERS.with_scale("10m"),
        linewidth=0.55, linestyle=":", edgecolor="#9d978b", zorder=4,
    )
    try:
        ax.add_feature(
            cfeature.STATES.with_scale("50m"),
            linewidth=0.32, edgecolor="#aaa49a", zorder=4,
        )
    except Exception:
        pass
    ax.coastlines("10m", linewidth=0.6, color="#4d5a52", zorder=5)

    # 广东省界单独加粗标黄（与 AIFS 站点一致）
    try:
        from cartopy.feature import ShapelyFeature

        guangdong = _guangdong_geometries()
        if guangdong:
            ax.add_feature(ShapelyFeature(
                guangdong, ccrs.PlateCarree(), facecolor="none",
                edgecolor="#6b5a00", linewidth=4.0, alpha=0.35, zorder=9,
            ))
            ax.add_feature(ShapelyFeature(
                guangdong, ccrs.PlateCarree(), facecolor="none",
                edgecolor="#FFE000", linewidth=2.4, zorder=10,
            ))
    except Exception:
        pass

    step = 1 if domain != "ea" else 10
    xlocs = np.arange(np.ceil(d["west"] / step) * step, d["east"] + 0.1, step)
    ylocs = np.arange(np.ceil(d["south"] / step) * step, d["north"] + 0.1, step)
    gl = ax.gridlines(
        draw_labels=True, xlocs=xlocs, ylocs=ylocs,
        linewidth=0.35, color="#9aa4ad", linestyle="--",
        alpha=0.5, zorder=2,
    )
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 8}
    gl.ylabel_style = {"size": 8}

    # (经度, 纬度, 标签横向偏移, 标签纵向偏移)，手动错开港澳和台北，
    # 避免小图上标签叠在一起。
    cities = {
        "Guangzhou": (113.26, 23.13, 0.10, 0.12),
        "Hong Kong": (114.17, 22.32, 0.14, -0.30),
        "Macau": (113.54, 22.20, -0.55, 0.18),
        "Haikou": (110.20, 20.04, 0.10, 0.10),
        "Sanya": (109.51, 18.25, 0.10, 0.08),
    }
    if domain in ("wave", "scs"):
        cities["Taipei"] = (121.56, 25.03, -0.72, 0.10)
    if domain == "scs":
        cities["Manila"] = (120.98, 14.60, 0.10, 0.08)
        cities["Da Nang"] = (108.22, 16.05, 0.10, 0.08)
    if domain == "ea":
        cities.update({
            "Beijing": (116.40, 39.90, 0.10, 0.10),
            "Shanghai": (121.47, 31.23, 0.10, 0.10),
            "Seoul": (126.98, 37.57, 0.10, 0.10),
            "Tokyo": (139.69, 35.68, -0.75, 0.10),
            "Taipei": (121.56, 25.03, -0.72, 0.10),
            "Manila": (120.98, 14.60, 0.10, 0.08),
            "Hanoi": (105.85, 21.03, 0.10, 0.10),
        })
    for name, (x, y, dx, dy) in cities.items():
        ax.plot(x, y, "o", ms=3, color="#222",
                transform=ccrs.PlateCarree(), zorder=11)
        ax.text(x + dx, y + dy, name, fontsize=6.5, color="#222",
                transform=ccrs.PlateCarree(), zorder=11)
    return ax


def finish_figure(fig, ax, cf, colorbar_ticks, colorbar_labels, colorbar_title,
                  title: str, valid: datetime, run_dt: datetime, lead: int,
                  out_path: Path, note: str = "") -> None:
    fig.text(0.065, 0.955, title, fontsize=16.5, fontweight="bold",
             ha="left", va="top")
    fig.text(0.955, 0.955, valid.strftime("%Y/%m/%d %H(UTC)"),
             fontsize=11, ha="right", va="top")
    fig.text(0.065, 0.923,
             "ECMWF WAM 0.25°" + ("  ·  " + note if note else ""),
             fontsize=9.5, color="#555", ha="left", va="top")
    fig.text(0.065, 0.032,
             "Init: %s  ·  Lead +%d h" % (
                 run_dt.strftime("%Y-%m-%d %H UTC"), lead),
             fontsize=8, color="#666", ha="left", va="bottom")
    fig.text(0.955, 0.032, "www.bamc.top", fontsize=11, color="#000000",
             ha="right", va="bottom")
    cax = fig.add_axes([0.885, 0.16, 0.022, 0.62])
    cb = fig.colorbar(cf, cax=cax, ticks=colorbar_ticks)
    cb.ax.tick_params(labelsize=7.5, length=2)
    cb.set_ticklabels(colorbar_labels)
    cb.set_label(colorbar_title, fontsize=8, labelpad=6)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, facecolor="white")


def render_frame(arrays: dict, grid: dict, run_dt: datetime, lead: int,
                 spec: dict, out_path: Path) -> None:
    import cartopy

    cartopy.config["data_dir"] = str(BASE_DIR / ".cartopy")

    import matplotlib
    matplotlib.use("Agg")
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    valid = run_dt + timedelta(hours=lead)
    domain = spec.get("domain", DEFAULT_DOMAIN)
    LAT, LON = np.meshgrid(grid["lat"], grid["lon"], indexing="ij")

    fig = Figure(figsize=(10, 7.2), dpi=140, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0.065, 0.075, 0.80, 0.80],
                      projection=ccrs.PlateCarree())
    base_map(ax, ccrs, cfeature, domain)
    note = spec.get("note", "")

    swh = smooth(arrays["swh"], 0.5)
    mwd = np.asarray(arrays["mwd"], dtype=np.float64)

    cf = ax.contourf(LON, LAT, swh, levels=SWH_LEVELS, colors=SWH_COLORS,
                     extend="max", transform=ccrs.PlateCarree(),
                     zorder=2, antialiased=True)
    cf.cmap.set_under("white")
    cf.cmap.set_over(SWH_OVER)

    cs = ax.contour(LON, LAT, swh, levels=SWH_CONTOURS,
                    colors="#26323c", linewidths=0.55,
                    transform=ccrs.PlateCarree(), zorder=6)
    ax.clabel(cs, inline=True, fontsize=6.3, fmt="%g", colors="#1f2a33")

    # 浪向：mwd 是「浪从哪个方向来」，箭头画传播方向（去向），即 +180°。
    theta = np.deg2rad(mwd)
    u = -np.sin(theta)
    v = -np.cos(theta)
    valid_arrow = np.isfinite(swh) & np.isfinite(mwd) & (swh >= 0.15)
    sample = ARROW_SAMPLE.get(domain, 4)
    sl = (slice(None, None, sample), slice(None, None, sample))
    lon_a, lat_a = LON[sl], LAT[sl]
    u_a, v_a = u[sl], v[sl]
    mask_a = ~valid_arrow[sl]
    u_a = np.ma.masked_where(mask_a, u_a)
    v_a = np.ma.masked_where(mask_a, v_a)
    base_q = dict(transform=ccrs.PlateCarree(), angles="xy",
                  scale_units="inches", scale=ARROW_SCALE,
                  pivot="middle", zorder=7)
    # 白色描边 + 深色箭头，保证在浅蓝和深红底图上都看得清。
    ax.quiver(lon_a, lat_a, u_a, v_a, color="white", alpha=0.75,
              width=0.0038, headwidth=4.2, headlength=5.0,
              headaxislength=4.6, **base_q)
    ax.quiver(lon_a, lat_a, u_a, v_a, color="#111827",
              width=0.0018, headwidth=4.2, headlength=5.0,
              headaxislength=4.6, **base_q)

    finish_figure(fig, ax, cf, SWH_LEVELS, SWH_LABELS,
                  "significant wave height (m)",
                  spec["title"], valid, run_dt, lead, out_path, note)
    fig.clf()


def make_overview(run_dt: datetime, panel_dir: Path, prefix: str,
                  out_path: Path) -> Path | None:
    """把 +12h…+240h 每 12h 一帧拼成 4×5 共 20 个小图的总览。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.cm import ScalarMappable
    from matplotlib.figure import Figure

    panels = []
    for step in OVERVIEW_STEPS:
        valid = run_dt + timedelta(hours=step)
        path = panel_dir / (prefix + valid.strftime("%Y%m%d%H") + ".png")
        if path.exists():
            panels.append((step, path))
    if not panels:
        log("没有可用于总览的单帧图")
        return None
    if len(panels) < len(OVERVIEW_STEPS):
        log("总览缺少 %d 个时次，按已有 %d 个拼图" % (
            len(OVERVIEW_STEPS) - len(panels), len(panels)))

    fig = Figure(figsize=(13.2, 10.2), dpi=150, facecolor="white")
    FigureCanvasAgg(fig)
    fig.text(0.035, 0.975,
             "ECMWF WAM · Significant Wave Height & Direction",
             fontsize=15, fontweight="bold", ha="left", va="top")
    fig.text(0.975, 0.975,
             "%s UTC  ·  +12–240 h" % run_dt.strftime("%Y-%m-%d %H"),
             fontsize=10.5, ha="right", va="top")

    # 单帧图 1400×1008，地图轴 rect=[0.065, 0.075, 0.80, 0.80]。
    x0, x1 = int(0.065 * 1400), int(0.865 * 1400)
    y0, y1 = int((1 - 0.875) * 1008), int((1 - 0.075) * 1008)

    cols, rows = 5, 4
    left, right = 0.035, 0.975
    gap = 0.012
    width = (right - left - gap * (cols - 1)) / cols
    height = width * (13.2 / 10.2) / ((x1 - x0) / (y1 - y0))
    top_y = 0.90
    row_gap = 0.025
    for index, (step, path) in enumerate(panels[:cols * rows]):
        row, col = divmod(index, cols)
        y = top_y - (row + 1) * height - row * row_gap
        ax = fig.add_axes([left + col * (width + gap), y, width, height])
        ax.imshow(mpimg.imread(path)[y0:y1, x0:x1])
        ax.set_axis_off()
        ax.set_title("+%dh" % step, fontsize=9.5, loc="left", pad=2,
                     color="#111111")

    cmap = ListedColormap(SWH_COLORS)
    cmap.set_over(SWH_OVER)
    norm = BoundaryNorm(SWH_LEVELS, cmap.N)
    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cax = fig.add_axes([0.28, 0.09, 0.44, 0.012])
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal",
                      ticks=SWH_LEVELS, extend="max")
    cb.ax.tick_params(labelsize=8, length=2)
    cb.set_ticklabels(SWH_LABELS)
    cb.set_label("significant wave height (m)", fontsize=8, labelpad=3)
    fig.text(0.975, 0.015, "www.bamc.top", fontsize=10,
             ha="right", va="bottom")
    fig.text(0.035, 0.015,
             "Init: %s UTC" % run_dt.strftime("%Y-%m-%d %H"),
             fontsize=7.5, color="#666", ha="left", va="bottom")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, facecolor="white")
    fig.clf()
    log("总览图 → %s" % out_path.name)
    return out_path


def write_overview_manifest(run_dt: datetime, out_dir: Path,
                            domain: str) -> Path:
    """总览产品每次起报只有一张图，manifest 里放一帧。"""
    stamp = run_dt.strftime("%Y%m%d%H")
    payload = {
        "product": "overview",
        "run": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
        "run_iso": run_dt.isoformat(),
        "generated": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M HKT"),
        "source": "ECMWF IFS-WAM 0.25° (Open Data)",
        "region": dict(DOMAINS[domain]),
        "frames": [{
            "s": stamp,
            "valid_utc": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "lead": 0,
            "file": "overview.png",
        }],
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def build_overview(run_dt: datetime, out_dir: Path, domain: str) -> Path | None:
    """从 swh 单帧图拼 20 小图总览，并写 overview 产品 manifest。"""
    panel_dir = out_dir / PRODUCTS["swh"]["subdir"]
    overview_dir = out_dir / PRODUCTS["overview"]["subdir"]
    path = make_overview(run_dt, panel_dir,
                         PRODUCTS["swh"]["prefix"],
                         overview_dir / "overview.png")
    if path is not None:
        write_overview_manifest(run_dt, overview_dir, domain)
    return path


def write_manifest(run_dt: datetime, out_dir: Path, product_id: str,
                   domain: str) -> Path:
    """按目录里已有的图片重建 manifest（字段与 AIFS 站点一致）。"""
    spec = PRODUCTS[product_id]
    frames = []
    for path in out_dir.glob(spec["prefix"] + "*.png"):
        stamp = path.stem[len(spec["prefix"]):]
        try:
            valid = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        lead = int((valid - run_dt).total_seconds() // 3600)
        if 0 <= lead <= 24 * 14:
            frames.append({
                "s": stamp,
                "valid_utc": valid.strftime("%Y-%m-%d %H:%M UTC"),
                "lead": lead,
                "file": path.name,
            })
    frames.sort(key=lambda item: item["lead"])
    payload = {
        "product": product_id,
        "run": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
        "run_iso": run_dt.isoformat(),
        "generated": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M HKT"),
        "source": "ECMWF IFS-WAM 0.25° (Open Data)",
        "region": dict(DOMAINS[domain]),
        "frames": frames,
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def render_all(run_dt: datetime, steps: dict[int, Path], out_dir: Path,
               products: list[str], domain: str,
               force: bool = False) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for product_id in products:
        if product_id == "overview":
            continue
        spec = dict(PRODUCTS[product_id])
        spec["domain"] = domain
        product_dir = out_dir / spec["subdir"]
        product_dir.mkdir(parents=True, exist_ok=True)
        for step, npz_path in steps.items():
            valid = run_dt + timedelta(hours=step)
            out_path = product_dir / (
                spec["prefix"] + valid.strftime("%Y%m%d%H") + ".png")
            if out_path.exists() and not force:
                log("+%dh 图片已存在，跳过（--force 可重画）" % step)
                written.append(out_path)
                continue
            started = time.time()
            arrays, grid = load_step(npz_path)
            render_frame(arrays, grid, run_dt, step, spec, out_path)
            log("+%dh 出图完成 %.1fs → %s" % (
                step, time.time() - started, out_path.name))
            written.append(out_path)
        write_manifest(run_dt, product_dir, product_id, domain)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ECMWF 海浪模式（WAM）有效波高 + 浪向预报图")
    parser.add_argument("--run", help="起报场，如 2026-11-17T00:00")
    parser.add_argument("--steps", help="只出这些时次，逗号分隔，如 24,48,72")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN,
                        choices=sorted(DOMAINS), help="作图海域")
    parser.add_argument("--products", default=",".join(ALL_PRODUCTS),
                        help="要生成的产品，逗号分隔")
    parser.add_argument("--out", help="输出根目录（默认 wave/）")
    parser.add_argument("--input", help="已缓存的 npz 目录")
    parser.add_argument("--jobs", type=int, default=4, help="下载并发数")
    parser.add_argument("--force", action="store_true", help="覆盖已有图片")
    parser.add_argument("--overview", action="store_true",
                        help="同时生成 20 小图总览产品（默认已包含）")
    parser.add_argument("--list-runs", action="store_true",
                        help="只列出最近的起报场状态")
    args = parser.parse_args()

    if args.list_runs:
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        anchor = now.replace(hour=12) if now.hour >= 12 else now.replace(
            hour=0)
        for offset in range(0, 72, 12):
            candidate = anchor - timedelta(hours=offset)
            if candidate.hour not in RUN_HOURS:
                continue
            print(candidate.strftime("%Y-%m-%dT%H:%M"), run_state(candidate))
        return

    run_label = args.run or latest_run()
    run_dt = datetime.strptime(run_label, "%Y-%m-%dT%H:%M").replace(
        tzinfo=UTC)
    log("起报场 %s" % run_dt.strftime("%Y-%m-%d %H UTC"))

    if args.steps:
        steps_wanted = sorted({int(s) for s in args.steps.split(",") if s.strip()})
    else:
        steps_wanted = LEADS

    selected = [x.strip() for x in args.products.split(",") if x.strip()]
    if args.overview and "overview" not in selected:
        selected.append("overview")
    unknown = [x for x in selected if x not in PRODUCTS]
    if unknown:
        raise SystemExit("未知产品：%s" % ", ".join(unknown))

    out_dir = Path(args.out) if args.out else OUT_DIR

    if args.input:
        cache_dir = Path(args.input)
        steps: dict[int, Path] = {}
        for step in steps_wanted:
            path = cache_dir / ("wave_%s_%03d.npz" % (
                run_dt.strftime("%Y%m%d%H"), step))
            if path.exists():
                steps[step] = path
            else:
                log("缺少缓存 +%dh，跳过" % step)
    else:
        steps = build_dataset(run_dt, steps_wanted,
                              workers=max(1, args.jobs), reuse=True)

    if not steps:
        log("没有可绘制的时次")
        return
    written = render_all(run_dt, steps, out_dir, selected, args.domain,
                         force=args.force)
    if "overview" in selected:
        build_overview(run_dt, out_dir, args.domain)
    log("完成：%d 张图 → %s" % (len(written), out_dir))


if __name__ == "__main__":
    main()
