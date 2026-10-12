# settings.py
import json
import os
import logging

class Settings:
    """管理用户配置的加载、保存和访问"""

    def __init__(self, filename='settings.json'):
        self.filename = filename
        self.data = {
            'names': {},          # 插件显示名称映射
            'plugin_order': {},   # 插件显示顺序
            'window_pos': None,   # 窗口位置 [x, y]
        }
        self.load()

    def load(self):
        """从文件加载配置，失败时保留默认值"""
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        # 只更新已有键，避免意外键覆盖
                        for key in list(self.data.keys()):
                            if key in loaded:
                                self.data[key] = loaded[key]
            except Exception as e:
                logging.warning(f"加载配置文件失败: {e}")

    def save(self):
        """保存配置到文件，失败时记录日志"""
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"保存配置文件失败: {e}")

    # ---------- 显示名称 ----------
    def get_display_name(self, original):
        """获取插件的显示名称，若未自定义则返回原名"""
        return self.data.get('names', {}).get(original, original)

    def set_display_name(self, original, new_name):
        """设置插件的显示名称"""
        self.data.setdefault('names', {})[original] = new_name
        self.save()

    # ---------- 插件顺序 ----------
    def get_plugin_order(self, app_id=''):
        """读某个 app 的插件顺序。app_id 为空时返回空列表。"""
        if not app_id:
            return []
        all_orders = self.data.get('plugin_order', {})
        if not isinstance(all_orders, dict):
            # 兼容旧的扁平数组格式（v1.0），忽略
            return []
        return list(all_orders.get(app_id, []) or [])

    def set_plugin_order(self, app_id, order):
        """写某个 app 的插件顺序。"""
        if not app_id:
            return
        all_orders = self.data.get('plugin_order')
        if not isinstance(all_orders, dict):
            # 旧格式 → 升级成 dict，旧数据丢弃
            all_orders = {}
            self.data['plugin_order'] = all_orders
        all_orders[app_id] = list(order or [])
        self.save()