# host/plugin_loader.py
"""
PluginLoader —— 扫描名片目录，读出名片内容

引擎不认识"游戏"。它只做：
    1. 扫 plugins/ 下有哪些目录
    2. 每个目录里有没有 config.json（名片）
    3. 有名片就把名片读出来

缓存策略（Steam 式）：
    - 启动时 warmup() 预扫一遍，所有 config.json 入缓存
    - 之后每次 read_config() 对比 mtime：
        没变 → 用缓存
        变了 → 重读，更新缓存
    - 解绑/重绑不影响缓存（缓存是"已安装列表"，不是"运行态"）
    - 新加/删业务目录，下次 list_apps() 扫描时自然反映

字段名全部中性：
    app / app_id —— 一份名片 + 它的目录名标识
    不出现 game / 游戏 这类词

依赖：无（只用标准库）
"""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    """名片解析出的任务条目（对应 config.json 里 tasks[] 的一项）"""
    task_id: str = ''
    name: str = ''
    class_name: str = ''
    plugin_module: str = ''
    app: str = ''            # 名片目录名（中性叫法，原来是 game）
    folder: str = ''
    stats: List[str] = field(default_factory=list)
    default_count: str = 'inf'


class PluginLoader:
    def __init__(self, base_dir=None):
        if base_dir is None:
            base_dir = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
        self._base_dir = base_dir
        self._plugins_dir = os.path.join(base_dir, 'plugins')

        # 缓存：app_id -> (mtime, cfg)
        # mtime 是 config.json 的修改时间戳；对比发现变了就重读
        self._config_cache: Dict[str, Tuple[float, dict]] = {}

    @property
    def plugins_dir(self):
        return self._plugins_dir

    # ==================================================================
    #  预缓存（启动时调用一次）
    # ==================================================================

    def warmup(self) -> int:
        """
        扫一遍 plugins/ 下所有业务目录，逐个 read_config 填缓存。
        返回成功读入的业务数量。
        """
        app_ids = self.list_apps()
        count = 0
        for app_id in app_ids:
            if self.read_config(app_id) is not None:
                count += 1
        logger.info(
            'PluginLoader: warmup 完成，%d/%d 个业务已入缓存',
            count, len(app_ids))
        return count

    def invalidate(self, app_id: str) -> None:
        """手动让某个业务的缓存失效（下次 read_config 会重读）"""
        self._config_cache.pop(app_id, None)

    def invalidate_all(self) -> None:
        """清空全部缓存"""
        self._config_cache.clear()

    # ==================================================================
    #  扫描目录
    # ==================================================================

    def list_apps(self):
        """列出所有含名片的目录名（原来叫 list_games）"""
        if not os.path.isdir(self._plugins_dir):
            return []
        result = []
        for name in sorted(os.listdir(self._plugins_dir)):
            full = os.path.join(self._plugins_dir, name)
            if not os.path.isdir(full):
                continue
            if name.startswith('_') or name.startswith('.'):
                continue
            if os.path.isfile(os.path.join(full, 'config.json')):
                result.append(name)
        return result

    # ==================================================================
    #  读名片（带缓存 + mtime 校验）
    # ==================================================================

    def read_config(self, app_id):
        """
        读某份名片。app_id 是目录名（原来参数叫 game）。

        缓存逻辑：
            文件不存在 → 清缓存，返回 None
            缓存命中且 mtime 未变 → 直接返回缓存
            缓存未命中或 mtime 变了 → 重读，更新缓存
        """
        path = os.path.join(self._plugins_dir, app_id, 'config.json')

        if not os.path.isfile(path):
            # 文件没了 → 清缓存
            self._config_cache.pop(app_id, None)
            return None

        try:
            mtime = os.path.getmtime(path)
        except Exception:
            logger.exception('PluginLoader: 取 mtime 失败 %s', path)
            return None

        # 缓存命中且 mtime 未变
        cached = self._config_cache.get(app_id)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        # 未命中 或 mtime 变了 → 重读
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            logger.exception('PluginLoader: 读名片失败 %s', path)
            return None

        self._config_cache[app_id] = (mtime, cfg)
        return cfg

    # ==================================================================
    #  解析任务（走缓存）
    # ==================================================================

    def scan(self, app_id):
        """把名片里的 tasks[] 解析成 PluginInfo 列表"""
        cfg = self.read_config(app_id)
        if cfg is None:
            return []
        result = []
        for t in cfg.get('tasks', []) or []:
            folder = t.get('folder', '')
            module_file = t.get('module_file', '')
            class_name = t.get('class', '')
            if not (folder and module_file and class_name):
                continue
            module_name = module_file.replace('.py', '')
            result.append(PluginInfo(
                task_id=t.get('id', ''),
                name=t.get('name', folder),
                class_name=class_name,
                plugin_module=f'plugins.{app_id}.{folder}.{module_name}',
                app=app_id,
                folder=folder,
                stats=list(t.get('stats', []) or []),
                default_count=t.get('default_count', 'inf'),
            ))
        return result

    def get_by_name(self, app_id, name):
        for info in self.scan(app_id):
            if info.name == name:
                return info
        return None