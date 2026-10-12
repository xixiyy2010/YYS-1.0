# capability/image_center.py
"""
ImageCenter —— 多基准模板管理器（机床能力）

改动要点（v2）：
    1. 不再写死项目根目录的 templates/；改为从 resource_paths 读
    2. 业务层启动时通过 resource_paths.set_templates_dir() 告诉机床
    3. 未设置时，所有路径相关函数返回 None / 空列表，并记警告

职责（不变）：
    1. 加载 templates/source_<base>.json
    2. 按当前截图分辨率，对基准模板的 crop 区域和模板图片做分轴缩放
    3. match() / compute_click_pos() 返回基准坐标
    4. 网格展开 / 采样点

依赖：capability.resource_paths（只读，不反向依赖）
"""
import os
import json
import math
import logging
import random
import threading
from collections import OrderedDict

import cv2
import numpy as np

from . import resource_paths


DEFAULT_BASE_NAME = '4k'
BASE_RESOLUTION = (3840, 2160)


# ==================================================================
#  templates 目录（动态，由业务层设置）
# ==================================================================

def _tpl_dir():
    """读当前 templates 目录绝对路径，未设置返回空串"""
    return resource_paths.get_templates_dir() or ''


def base_json_path(base_name):
    d = _tpl_dir()
    if not d:
        return ''
    return os.path.join(d, f'source_{base_name}.json')


def base_source_dir(base_name):
    d = _tpl_dir()
    if not d:
        return ''
    return os.path.join(d, f'source_{base_name}')


def base_cache_dir(base_name):
    d = _tpl_dir()
    if not d:
        return ''
    return os.path.join(d, 'cache', f'source_{base_name}')


def list_available_bases():
    """列出 templates 下所有可用的基准名（从 source_*.json 扫描）"""
    d = _tpl_dir()
    if not d or not os.path.isdir(d):
        return []
    result = []
    for f in os.listdir(d):
        if f.startswith('source_') and f.endswith('.json'):
            name = f[len('source_'):-len('.json')]
            if name:
                result.append(name)
    return result


def _read_base_resolution(base_name):
    """读某个基准的 meta.resolution，读不到返回 None"""
    path = base_json_path(base_name)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        res = data.get('meta', {}).get('resolution')
        if res and len(res) == 2:
            return (int(res[0]), int(res[1]))
    except Exception as e:
        logging.warning(f'image_center: 读取 {path} 失败: {e}')
    return None


def _pick_base_by_height(target_res=None):
    """按高度差挑最接近的基准名。未设置 templates 目录时返回 None。"""
    avail = list_available_bases()
    if not avail:
        return None

    if target_res is None:
        best = None
        best_h = -1
        for name in avail:
            res = _read_base_resolution(name)
            if res and res[1] > best_h:
                best_h = res[1]
                best = name
        return best or avail[0]

    target_h = int(target_res[1])
    best = None
    best_diff = None
    for name in avail:
        res = _read_base_resolution(name)
        if not res:
            continue
        diff = abs(res[1] - target_h)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = name
    return best or avail[0]


def _derive_base_name(index_path):
    """从 index_path 推导 base_name"""
    stem = os.path.splitext(os.path.basename(index_path))[0]
    if stem.startswith('source_'):
        return stem[len('source_'):]
    return DEFAULT_BASE_NAME


# ---------- 采样工具（不变） ----------

def _gauss_in_rect(cx, cy, w, h, sigma_div=6):
    sigma_x = max(1.0, w / sigma_div)
    sigma_y = max(1.0, h / sigma_div)
    return random.gauss(cx, sigma_x), random.gauss(cy, sigma_y)


def _clamp_rect(px, py, x1, y1, x2, y2):
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    lo_y, hi_y = (y1, y2) if y1 <= y2 else (y2, y1)
    px = max(lo_x + 1, min(hi_x - 1, px))
    py = max(lo_y + 1, min(hi_y - 1, py))
    return int(px), int(py)


def _jitter_fixed(x, y, sigma=2.0, max_offset=5):
    px = x + random.gauss(0, sigma)
    py = y + random.gauss(0, sigma)
    px = max(x - max_offset, min(x + max_offset, px))
    py = max(y - max_offset, min(y + max_offset, py))
    return int(px), int(py)


# ---------- 常量 ----------
MATCH_FIXED = 'fixed'
MATCH_SEARCH = 'search'
MATCH_CUSTOM = 'custom'

CLICK_NONE = ''
CLICK_CENTER = 'center'
CLICK_POINT = 'point'
CLICK_REGION = 'region'
CLICK_REGION_CENTER = 'region_center'

INTERPOLATIONS = {}
for _name, _attr in [
    ('NEAREST', 'INTER_NEAREST'),
    ('LINEAR', 'INTER_LINEAR'),
    ('CUBIC', 'INTER_CUBIC'),
    ('AREA', 'INTER_AREA'),
    ('LANCZOS4', 'INTER_LANCZOS4'),
    ('LINEAR_EXACT', 'INTER_LINEAR_EXACT'),
    ('NEAREST_EXACT', 'INTER_NEAREST_EXACT'),
]:
    if hasattr(cv2, _attr):
        INTERPOLATIONS[_name] = getattr(cv2, _attr)

DEFAULT_INTERP = 'NEAREST_EXACT'
DEFAULT_ROUND = 'floor'

if DEFAULT_INTERP not in INTERPOLATIONS and 'NEAREST' in INTERPOLATIONS:
    INTERPOLATIONS[DEFAULT_INTERP] = INTERPOLATIONS['NEAREST']


def _scale_axis(v, base, target, mode):
    x = v * target / base
    if mode == 'floor':
        return int(math.floor(x))
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


# ---------- 网格 / 滑动（不变） ----------

def expand_grid(cfg):
    w, h = cfg['w'], cfg['h']

    if cfg.get('x_list'):
        xs = list(cfg['x_list'])
    else:
        xs = [cfg['x_start'] + i * (w + cfg['gap_x'])
              for i in range(cfg['cols'])]

    if cfg.get('y_list'):
        ys = list(cfg['y_list'])
    else:
        ys = [cfg['y_start'] + i * (h + cfg['gap_y'])
              for i in range(cfg['rows'])]

    cells = []
    for y in ys:
        for x in xs:
            cells.append([x, y, x + w, y + h])
    return cells


def grid_sample_point(cell, cc):
    x1, y1, x2, y2 = cell
    corner = cc.get('corner', 'top_right')
    if corner == 'top_left':
        bx, by = x1, y1
    elif corner == 'top_right':
        bx, by = x2, y1
    elif corner == 'bottom_left':
        bx, by = x1, y2
    elif corner == 'bottom_right':
        bx, by = x2, y2
    else:
        bx, by = (x1 + x2) // 2, (y1 + y2) // 2
    return bx + cc.get('offset_x', 0), by + cc.get('offset_y', 0)


def compute_swipe_paths(entry):
    sr = entry.get('start_region')
    er = entry.get('end_region')
    if not sr or len(sr) != 4 or not er or len(er) != 4:
        return None

    sx1, sy1, sx2, sy2 = sr
    ex1, ey1, ex2, ey2 = er
    SX_min, SX_max = min(sx1, sx2), max(sx1, sx2)
    SY_min, SY_max = min(sy1, sy2), max(sy1, sy2)
    EX_min, EX_max = min(ex1, ex2), max(ex1, ex2)
    EY_min, EY_max = min(ey1, ey2), max(ey1, ey2)

    dx_a = int(entry.get('dx_min', 0)); dx_b = int(entry.get('dx_max', 0))
    dy_a = int(entry.get('dy_min', 0)); dy_b = int(entry.get('dy_max', 0))
    dx_min_abs, dx_max_abs = min(dx_a, dx_b), max(dx_a, dx_b)
    dy_min_abs, dy_max_abs = min(dy_a, dy_b), max(dy_a, dy_b)

    sx_c = (SX_min + SX_max) / 2
    sy_c = (SY_min + SY_max) / 2
    ex_c = (EX_min + EX_max) / 2
    ey_c = (EY_min + EY_max) / 2
    dx_sign = 1 if ex_c >= sx_c else -1
    dy_sign = 1 if ey_c >= sy_c else -1

    if dx_min_abs == 0 and dx_max_abs == 0:
        sx = random.randint(SX_min, SX_max)
        ex = random.randint(EX_min, EX_max)
    else:
        if dx_sign > 0:
            eff_dx_min, eff_dx_max = dx_min_abs, dx_max_abs
        else:
            eff_dx_min, eff_dx_max = -dx_max_abs, -dx_min_abs
        new_SX_min = max(SX_min, EX_min - eff_dx_min)
        new_SX_max = min(SX_max, EX_max - eff_dx_max)
        if new_SX_min > new_SX_max:
            new_SX_min, new_SX_max = SX_min, SX_max
        sx = random.randint(new_SX_min, new_SX_max)
        ex = sx + random.randint(eff_dx_min, eff_dx_max)

    if dy_min_abs == 0 and dy_max_abs == 0:
        sy = random.randint(SY_min, SY_max)
        ey = random.randint(EY_min, EY_max)
    else:
        if dy_sign > 0:
            eff_dy_min, eff_dy_max = dy_min_abs, dy_max_abs
        else:
            eff_dy_min, eff_dy_max = -dy_max_abs, -dy_min_abs
        new_SY_min = max(SY_min, EY_min - eff_dy_min)
        new_SY_max = min(SY_max, EY_max - eff_dy_max)
        if new_SY_min > new_SY_max:
            new_SY_min, new_SY_max = SY_min, SY_max
        sy = random.randint(new_SY_min, new_SY_max)
        ey = sy + random.randint(eff_dy_min, eff_dy_max)

    nodes_offset = entry.get('nodes_offset') or []
    n = len(nodes_offset)
    random_px = max(0, int(entry.get('random_px', 0)))

    points = [(sx, sy)]
    for i in range(n):
        t = (i + 1) / (n + 1)
        px = sx + t * (ex - sx) + int(nodes_offset[i][0])
        py = sy + t * (ey - sy) + int(nodes_offset[i][1])
        points.append((px, py))
    points.append((ex, ey))

    if random_px > 0:
        points = [(p[0] + random.randint(-random_px, random_px),
                   p[1] + random.randint(-random_px, random_px))
                  for p in points]

    paths = []
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        paths.append((int(round(ax)), int(round(ay)),
                      int(round(bx)), int(round(by))))

    return {
        'paths': paths,
        'duration_ms': int(entry.get('duration_ms', 300)),
        'interval_ms': int(entry.get('interval_ms', 50)),
    }


# ==================================================================
#  TemplateManager
# ==================================================================

class TemplateManager:
    """
    多基准模板管理器。

    构造：
        TemplateManager()                          # 取最高基准兜底
        TemplateManager(target_res=(1920,1080))    # 按高度选最近的基准
        TemplateManager(base_name='1080')          # 显式指定
        TemplateManager(index_path='...')          # 指定 json 路径

    注意：
        调用前必须保证 resource_paths.set_templates_dir() 已设置。
        未设置时构造的实例是空壳（所有匹配返回 None / 0）。
    """

    # 缓存上限（LRU 淘汰）
    _TEMPLATE_IMAGE_CACHE_MAX = 64      # 原图缓存：64 张，4K PNG ≈ 1.5GB 上限
    _SCALED_CACHE_MAX = 512             # 缩放图缓存：512 张

    def __init__(self, index_path=None, base_name=None, target_res=None):
        if index_path is not None:
            self.index_path = index_path
            self.base_name = _derive_base_name(index_path)
        elif base_name is not None:
            self.base_name = base_name
            self.index_path = base_json_path(base_name)
        else:
            picked = _pick_base_by_height(target_res)
            if picked is None:
                picked = DEFAULT_BASE_NAME
            self.base_name = picked
            self.index_path = base_json_path(picked)

        self.source_dir = base_source_dir(self.base_name)
        self.cache_dir = base_cache_dir(self.base_name)

        self.index = None
        self.base_res = BASE_RESOLUTION
        self.defaults = {}
        self._template_image_cache = OrderedDict()
        self._scaled_cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._priority_cache = None
        self._load_index()

    def _load_index(self):
        if not self.index_path or not os.path.exists(self.index_path):
            logging.warning(
                'image_center: 基准配置不存在: %s（可能还没设置 templates 目录）',
                self.index_path or '(未设置)')
            self.index = self._empty_index()
            return
        try:
            with open(self.index_path, 'r', encoding='utf-8') as f:
                self.index = json.load(f)
        except Exception as e:
            logging.error(
                f'image_center: 加载 {self.index_path} 失败: {e}')
            self.index = self._empty_index()
            return

        res = self.index.get('meta', {}).get('resolution')
        if res and isinstance(res, (list, tuple)) and len(res) == 2:
            self.base_res = (int(res[0]), int(res[1]))
        else:
            self.base_res = BASE_RESOLUTION

        defaults = self.index.get('defaults', {})
        required = ('threshold', 'padding', 'search_margin')
        missing = [k for k in required if k not in defaults]
        if missing:
            raise ValueError(
                f'image_center: {self.index_path} '
                f'defaults 缺少字段: {", ".join(missing)}')
        self.defaults = {k: defaults[k] for k in required}

    def _empty_index(self):
        return {
            'meta': {'resolution': list(self.base_res)},
            'defaults': dict(self.defaults),
            'templates': {}, 'regions': {}, 'points': {},
            'swipes': {}, 'coords': {}, 'grids': {},
        }

    def reload(self):
        with self._cache_lock:
            self._template_image_cache.clear()
            self._scaled_cache.clear()
        self._load_index()

    # ==================================================================
    #  缓存辅助（LRU，线程安全）
    # ==================================================================

    def _cache_get(self, cache: OrderedDict, key):
        """
        返回 (hit, value)。hit 为 True 表示缓存命中（value 可能是 None）。

        加锁原因：collaboration 任务用 ThreadPoolExecutor 并发调 match，
        两个线程同时访问同一 OrderedDict 会触发竞态（move_to_end +
        popitem 交错可能淘汰活跃 key）。
        """
        with self._cache_lock:
            if key in cache:
                cache.move_to_end(key)
                return True, cache[key]
            return False, None

    def _cache_put(self, cache: OrderedDict, key, value,
                   max_size: int) -> None:
        """写入并 move_to_end；超限淘汰最旧的。线程安全。"""
        with self._cache_lock:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > max_size:
                cache.popitem(last=False)

    def get_entry(self, plugin, name):
        return self.index.get('templates', {}).get(plugin, {}).get(name)

    def list_all(self):
        result = []
        for plugin, tmpls in self.index.get('templates', {}).items():
            for name, entry in tmpls.items():
                result.append((plugin, name, entry))
        return result

    # ==================================================================
    #  多模板优先级排序
    # ==================================================================

    def _priority_path(self):
        d = _tpl_dir()
        if not d:
            return ''
        return os.path.join(d, f'priority_{self.base_name}.json')

    def _load_priority(self):
        if self._priority_cache is not None:
            return self._priority_cache
        path = self._priority_path()
        if not path or not os.path.exists(path):
            self._priority_cache = {}
            return self._priority_cache
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception as e:
            logging.warning(f'image_center: 读优先级文件失败: {e}')
            data = {}
        self._priority_cache = data
        return data

    def _save_priority(self):
        """
        保留但当前不调用。

        E2 决策：优先级只写内存，不落盘。
        见 _bump_priority 注释。
        """
        if self._priority_cache is None:
            return
        path = self._priority_path()
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._priority_cache, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:
            logging.warning(f'image_center: 写优先级文件失败: {e}')

    def _collect_image_names(self, entry, plugin=None, name=None):
        default_order = []
        if entry.get('image'):
            default_order.append(entry['image'])
        for ei in entry.get('extra_images', []) or []:
            nm = ei.get('image')
            if nm and nm not in default_order:
                default_order.append(nm)

        if plugin is None or name is None:
            return default_order

        data = self._load_priority()
        saved = data.get(f'{plugin}/{name}')
        if not saved or not isinstance(saved, list):
            return default_order

        default_set = set(default_order)
        result = []
        for nm in saved:
            if nm in default_set and nm not in result:
                result.append(nm)
        for nm in default_order:
            if nm not in result:
                result.append(nm)
        return result

    def _bump_priority(self, plugin, name, image_name):
        entry = self.get_entry(plugin, name)
        if entry is None:
            return
        order = self._collect_image_names(entry, plugin, name)
        if image_name not in order:
            return

        new_order = [image_name] + [x for x in order if x != image_name]

        data = self._load_priority()
        key = f'{plugin}/{name}'
        if data.get(key) == new_order:
            return

        data[key] = new_order
        # 只改内存，不落盘。priority_4k.json 作为"初始顺序"只读。
        # 进程重启后首次遇到多模板组，从头试一遍（几十毫秒，一次）。

    # ==================================================================
    #  图片加载 / 缩放
    # ==================================================================

    def _get_source_image(self, plugin, name, image_name=None):
        entry = self.get_entry(plugin, name)
        if entry is None:
            return None
        if not image_name:
            image_name = entry.get('image')
        if not image_name:
            return None

        key = (plugin, name, image_name)
        hit, cached = self._cache_get(self._template_image_cache, key)
        if hit:
            return cached

        if not self.source_dir:
            logging.warning('image_center: source_dir 未设置')
            self._cache_put(self._template_image_cache, key, None,
                            self._TEMPLATE_IMAGE_CACHE_MAX)
            return None

        img_path = os.path.join(self.source_dir, image_name)
        if not os.path.exists(img_path):
            logging.warning(f'image_center: 模板图不存在: {img_path}')
            self._cache_put(self._template_image_cache, key, None,
                            self._TEMPLATE_IMAGE_CACHE_MAX)
            return None

        img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8),
                           cv2.IMREAD_COLOR)
        if img is None:
            logging.warning(f'image_center: 无法解码: {img_path}')
            self._cache_put(self._template_image_cache, key, None,
                            self._TEMPLATE_IMAGE_CACHE_MAX)
            return None

        self._cache_put(self._template_image_cache, key, img,
                        self._TEMPLATE_IMAGE_CACHE_MAX)
        return img

    def _get_scaled_image(self, plugin, name, target_w, target_h,
                          interp_name, round_mode, image_name=None):
        entry = self.get_entry(plugin, name)
        if entry is None:
            return None
        if not image_name:
            image_name = entry.get('image')
        if not image_name:
            return None

        key = (plugin, name, image_name, target_w, target_h,
               interp_name, round_mode)
        hit, cached = self._cache_get(self._scaled_cache, key)
        if hit:
            return cached

        main_image = entry.get('image')
        if image_name == main_image:
            cache_file = f'{name}.png'
        else:
            cache_file = f'{name}__{image_name}.png'
        cache_path = ''
        if self.cache_dir:
            cache_path = os.path.join(
                self.cache_dir, f'{target_w}x{target_h}',
                f'{interp_name}_{round_mode}', plugin, cache_file)

        if cache_path and os.path.exists(cache_path):
            try:
                disk_cached = cv2.imdecode(
                    np.fromfile(cache_path, dtype=np.uint8),
                    cv2.IMREAD_COLOR
                )
            except Exception:
                disk_cached = None
            if disk_cached is not None:
                self._cache_put(self._scaled_cache, key, disk_cached,
                                self._SCALED_CACHE_MAX)
                return disk_cached

        src_img = self._get_source_image(plugin, name, image_name)
        if src_img is None:
            self._cache_put(self._scaled_cache, key, None,
                            self._SCALED_CACHE_MAX)
            return None

        tmpl_h_base, tmpl_w_base = src_img.shape[:2]
        base_w, base_h = self.base_res

        new_w = _scale_axis(tmpl_w_base, base_w, target_w, round_mode)
        new_h = _scale_axis(tmpl_h_base, base_h, target_h, round_mode)
        if new_w <= 0 or new_h <= 0:
            self._cache_put(self._scaled_cache, key, None,
                            self._SCALED_CACHE_MAX)
            return None

        interp_flag = INTERPOLATIONS.get(
            interp_name,
            INTERPOLATIONS.get(DEFAULT_INTERP, cv2.INTER_LINEAR)
        )
        try:
            scaled = cv2.resize(src_img, (new_w, new_h),
                                interpolation=interp_flag)
        except Exception as e:
            logging.warning(f'image_center: 缩放失败 {plugin}/{name}: {e}')
            self._cache_put(self._scaled_cache, key, None,
                            self._SCALED_CACHE_MAX)
            return None

        if cache_path:
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                ok, buf = cv2.imencode('.png', scaled)
                if ok:
                    buf.tofile(cache_path)
            except Exception as e:
                logging.warning(
                    f'image_center: 写缓存失败 {plugin}/{name}: {e}')

        self._cache_put(self._scaled_cache, key, scaled,
                        self._SCALED_CACHE_MAX)
        return scaled

    def _get_algo(self, entry):
        interp = entry.get('recommended_interp') or DEFAULT_INTERP
        round_mode = entry.get('recommended_round') or DEFAULT_ROUND
        if interp not in INTERPOLATIONS:
            interp = DEFAULT_INTERP
        if round_mode not in ('round', 'floor'):
            round_mode = DEFAULT_ROUND
        return interp, round_mode

    def _get_threshold(self, entry):
        t = entry.get('threshold')
        if t is None:
            return self.defaults['threshold']
        return t

    def _screen_to_base(self, sx, sy, screen_w, screen_h):
        base_w, base_h = self.base_res
        bx = int(sx * base_w / screen_w)
        by = int(sy * base_h / screen_h)
        return bx, by

    # ==================================================================
    #  匹配
    # ==================================================================

    def _match_single(self, screen, plugin, name, entry, image_name,
                      interp_name, round_mode, match_mode,
                      screen_w, screen_h):
        tmpl_scaled = self._get_scaled_image(
            plugin, name, screen_w, screen_h,
            interp_name, round_mode, image_name
        )
        if tmpl_scaled is None:
            return 0.0, None

        tmpl_h, tmpl_w = tmpl_scaled.shape[:2]

        if match_mode == MATCH_SEARCH:
            roi = screen
            offset_x, offset_y = 0, 0
        elif match_mode == MATCH_CUSTOM:
            search_crop = entry.get('search_crop')
            if not search_crop:
                return 0.0, None
            base_w, base_h = self.base_res
            sx_factor = screen_w / base_w
            sy_factor = screen_h / base_h
            x1 = int(search_crop[0] * sx_factor)
            y1 = int(search_crop[1] * sy_factor)
            x2 = int(search_crop[2] * sx_factor)
            y2 = int(search_crop[3] * sy_factor)
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(screen_w, x2)
            y2 = min(screen_h, y2)
            if x2 - x1 < tmpl_w or y2 - y1 < tmpl_h:
                return 0.0, None
            roi = screen[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1
        else:
            crop_base = entry.get('crop')
            if not crop_base:
                return 0.0, None
            padding = entry.get('padding', self.defaults['padding'])
            base_w, base_h = self.base_res

            pad_sx = _scale_axis(padding, base_w, screen_w, round_mode)
            pad_sy = _scale_axis(padding, base_h, screen_h, round_mode)

            margin = entry.get('search_margin', self.defaults['search_margin'])
            m_x = _scale_axis(margin, base_w, screen_w, round_mode)
            m_y = _scale_axis(margin, base_h, screen_h, round_mode)

            x1 = _scale_axis(crop_base[0], base_w, screen_w, round_mode) - pad_sx - m_x
            y1 = _scale_axis(crop_base[1], base_h, screen_h, round_mode) - pad_sy - m_y
            x2 = _scale_axis(crop_base[2], base_w, screen_w, round_mode) + pad_sx + m_x
            y2 = _scale_axis(crop_base[3], base_h, screen_h, round_mode) + pad_sy + m_y

            if x2 - x1 < tmpl_w:
                x2 = x1 + tmpl_w
            if y2 - y1 < tmpl_h:
                y2 = y1 + tmpl_h

            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(screen_w, x2)
            y2 = min(screen_h, y2)

            if x2 - x1 < tmpl_w or y2 - y1 < tmpl_h:
                return 0.0, None

            roi = screen[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1

        if roi.shape[0] < tmpl_h or roi.shape[1] < tmpl_w:
            return 0.0, None

        try:
            result = cv2.matchTemplate(roi, tmpl_scaled, cv2.TM_CCOEFF_NORMED)
        except Exception as e:
            logging.warning(
                f'image_center: matchTemplate 失败 {plugin}/{name}: {e}')
            return 0.0, None

        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        screen_cx = offset_x + max_loc[0] + tmpl_w // 2
        screen_cy = offset_y + max_loc[1] + tmpl_h // 2
        return float(max_val), (screen_cx, screen_cy)

    def match(self, screen, plugin, name, return_center=True):
        if screen is None:
            return None if return_center else 0.0

        entry = self.get_entry(plugin, name)
        if entry is None:
            return None if return_center else 0.0

        screen_h, screen_w = screen.shape[:2]
        interp_name, round_mode = self._get_algo(entry)
        threshold = self._get_threshold(entry)
        match_mode = entry.get('match_mode', MATCH_FIXED)

        image_names = self._collect_image_names(entry, plugin, name)
        if not image_names:
            return None if return_center else 0.0

        best_score = 0.0
        for image_name in image_names:
            score, center = self._match_single(
                screen, plugin, name, entry, image_name,
                interp_name, round_mode, match_mode,
                screen_w, screen_h,
            )
            if score > best_score:
                best_score = score
            if score >= threshold and center is not None:
                self._bump_priority(plugin, name, image_name)
                if not return_center:
                    return score
                bx, by = self._screen_to_base(
                    center[0], center[1], screen_w, screen_h)
                return (bx, by)

        if not return_center:
            return best_score
        return None

    def exists(self, screen, plugin, name):
        entry = self.get_entry(plugin, name)
        if entry is None:
            return False
        return (self.match(screen, plugin, name, return_center=False)
                >= self._get_threshold(entry))

    def score(self, screen, plugin, name):
        return self.match(screen, plugin, name, return_center=False)

    # ==================================================================
    #  纯坐标 / 滑动 / 网格
    # ==================================================================

    def get_coord(self, plugin, name):
        return self.index.get('coords', {}).get(plugin, {}).get(name)

    def get_region(self, plugin, name):
        return self.index.get('regions', {}).get(plugin, {}).get(name)

    def get_swipe(self, plugin, name):
        return self.index.get('swipes', {}).get(plugin, {}).get(name)

    def get_grid(self, plugin, name):
        return self.index.get('grids', {}).get(plugin, {}).get(name)

    def click_coord(self, plugin, name, tap_func, normal_click_func=None):
        entry = self.get_coord(plugin, name)
        if entry is None:
            return False
        click_mode = entry.get('click_mode')
        click_data = entry.get('click')
        if not click_mode or not click_data:
            return False

        if click_mode == CLICK_POINT and len(click_data) == 2:
            x, y = _jitter_fixed(int(click_data[0]), int(click_data[1]))
            tap_func(x, y)
            return True

        if click_mode == CLICK_REGION and len(click_data) == 4:
            if normal_click_func is not None:
                x, y = normal_click_func(click_data)
            else:
                x1, y1, x2, y2 = click_data
                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                px, py = _gauss_in_rect(cx, cy, abs(x2 - x1), abs(y2 - y1),
                                        sigma_div=6)
                x, y = _clamp_rect(px, py, x1, y1, x2, y2)
            tap_func(int(x), int(y))
            return True

        if click_mode == CLICK_REGION_CENTER and len(click_data) == 4:
            x1, y1, x2, y2 = (int(click_data[0]), int(click_data[1]),
                              int(click_data[2]), int(click_data[3]))
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            px, py = _gauss_in_rect(cx, cy, abs(x2 - x1), abs(y2 - y1),
                                    sigma_div=6)
            x, y = _clamp_rect(px, py, x1, y1, x2, y2)
            tap_func(x, y)
            return True

        return False

    def compute_swipe_paths(self, plugin, name):
        entry = self.get_swipe(plugin, name)
        if entry is None:
            return None
        return compute_swipe_paths(entry)

    def compute_click_pos(self, plugin, name, screen=None, match_center=None):
        entry = self.get_entry(plugin, name)
        if entry is None:
            return None

        click_mode = entry.get('click_mode')
        click_data = entry.get('click')
        if not click_mode:
            return None

        if click_mode == CLICK_CENTER:
            if match_center is None:
                return None
            match_mode = entry.get('match_mode', MATCH_FIXED)
            if match_mode in (MATCH_SEARCH, MATCH_CUSTOM):
                return match_center
            crop = entry.get('crop')
            if not crop or len(crop) != 4:
                return match_center
            x1, y1, x2, y2 = crop
            cx, cy = match_center
            px, py = _gauss_in_rect(cx, cy, abs(x2 - x1), abs(y2 - y1),
                                    sigma_div=8)
            return _clamp_rect(px, py, x1, y1, x2, y2)

        if click_mode == CLICK_POINT:
            if not click_data or len(click_data) != 2:
                return None
            return _jitter_fixed(int(click_data[0]), int(click_data[1]))

        if click_mode == CLICK_REGION:
            if not click_data or len(click_data) != 4:
                return None
            return (int(click_data[0]), int(click_data[1]),
                    int(click_data[2]), int(click_data[3]))

        if click_mode == CLICK_REGION_CENTER:
            if not click_data or len(click_data) != 4:
                return None
            x1 = int(click_data[0]); y1 = int(click_data[1])
            x2 = int(click_data[2]); y2 = int(click_data[3])
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            px, py = _gauss_in_rect(cx, cy, abs(x2 - x1), abs(y2 - y1),
                                    sigma_div=6)
            return _clamp_rect(px, py, x1, y1, x2, y2)

        return None

    def match_and_click(self, screen, plugin, name, tap_func,
                        normal_click_func=None):
        center = self.match(screen, plugin, name, return_center=True)
        if center is None:
            return False

        pos = self.compute_click_pos(plugin, name, screen, match_center=center)
        if pos is None:
            return False

        if len(pos) == 2:
            x, y = pos
            tap_func(x, y)
            return True
        elif len(pos) == 4:
            region_base = pos
            if normal_click_func is not None:
                x, y = normal_click_func(region_base)
            else:
                x = (region_base[0] + region_base[2]) // 2
                y = (region_base[1] + region_base[3]) // 2
            tap_func(x, y)
            return True
        return False