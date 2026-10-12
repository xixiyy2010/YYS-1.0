# contracts/ui_protocol.py
"""
UI ↔ 引擎 通信协议（契约层）

这个文件只定义"名字"和"数据格式"，不实现任何逻辑。
引擎侧（host/engine_service.py）和 UI 侧（ui/engine_client.py）
都 import 它，保证两边说的是同一门语言。

通信模型：
    引擎进程（engine_main.py）  ←→  UI 进程（ui_main.py）
                       ↑
             命名管道 yys_ui_bridge
                       ↓
    每行一条 JSON：{"t": "cmd"|"evt", "n": "名字", "d": {...}}

    t = "cmd"  → UI 发给引擎的命令
    t = "evt"  → 引擎推给 UI 的事件

中立原则：
    引擎不认识具体业务。业务通过"座位"的方式，挂在 UI 上。
    业务在名片（config.json）里声明"我要坐哪个座位"。
    UI 提前把座位排好，只认座位号，不认业务是什么。

依赖：无
"""


# ==================================================================
#  管道名
# ==================================================================

UI_PIPE_NAME = 'yys_ui_bridge'


# ==================================================================
#  消息类型
# ==================================================================

TYPE_COMMAND = 'cmd'   # UI → 引擎
TYPE_EVENT = 'evt'     # 引擎 → UI


# ==================================================================
#  UI → 引擎  命令名
# ==================================================================

class UICommands:
    SYNC = 'sync'                        # 请求同步所有状态
    LIST_CANDIDATES = 'list_candidates'  # 列出候选目标窗口
    LIST_APPS = 'list_apps'              # 下拉框展开时，让引擎现扫一遍
    BIND = 'bind'                        # 绑定窗口
    UNBIND = 'unbind'                    # 解绑
    START_TASK = 'start_task'            # 启动任务
    STOP_TASK = 'stop_task'              # 停止任务
    SLOT_COMMAND = 'slot_command'        # 操作某个座位（比如按了开关）
    ACTIVATE_WINDOW = 'activate_window'  # 新增：让设备的父窗口获得焦点
    SHUTDOWN = 'shutdown'                # 关闭引擎


# ==================================================================
#  引擎 → UI  事件名
# ==================================================================

class UIEvents:
    SYNC_RESULT = 'sync_result'              # sync 的响应
    CANDIDATES_RESULT = 'candidates_result'  # list_candidates 的响应
    APPS_UPDATED = 'apps_updated'            # ← 加这行（下拉框展开时，引擎现扫的列表）
    DEVICE_CONNECTED = 'device_connected'
    DEVICE_DISCONNECTED = 'device_disconnected'
    BOSS_STATE = 'boss_state'                # 老板状态（运行/出错），UI 用它变标签页颜色
    SLOT_CHANGED = 'slot_changed'            # 某个座位的值变了
    LOG = 'log'


# ==================================================================
#  座位种类（UI 内部按这个识别怎么画）
# ==================================================================

class SlotKinds:
    BUSINESS = 'business'    # 业务位：显示业务名（下拉框）
    BIND = 'bind'            # 绑定位：固定"绑定"，不读名字
    ALERT = 'alert'          # 提醒位：没文字，只有颜色闪烁
    SWITCH = 'switch'        # 开关位：显示名字，可开可关


# ==================================================================
#  座位号（UI 内部对照用，业务写中文含义也行）
# ==================================================================

class SeatNumbers:
    BUSINESS = 'A'           # 业务位座位号
    BIND = 'B'               # 绑定位座位号
    ALERT = 'C'              # 提醒位座位号
    # 开关位：1、2、3、4…… 按名片声明顺序排


# ==================================================================
#  数据格式（文档，代码里用普通 dict）
# ==================================================================
"""
【名片（config.json）里怎么声明座位】

名片里加一段 "slots"，每个座位一个条目。
业务写清楚：座位号 + 中文含义 + 该带的参数。

举例（阴阳师的名片）：

"slots": {
  "business": {
    "seat": "A",
    "label": "阴阳师"
  },
  "bind": {
    "seat": "B",
    "parent": "MuMu安卓设备",
    "shot": {"by": "title", "value": "nemudisplay"},
    "click": {"by": "title", "value": "MuMuNxDevice"}
  },
  "alert": {
    "seat": "C",
    "label": "悬赏协助"
  },
  "switches": [
    {"seat": "1", "label": "全局处理", "default": true},
    {"seat": "2", "label": "掉线", "default": false}
  ]
}

说明：
  - "seat" 是座位号，给人看的（也方便 UI 对照）
  - "label" 是显示名，UI 原样显示，不解析
  - business / bind / alert 是唯一座位（各一个）
  - switches 是一排，有多少写多少，UI 按顺序排


【sync_result 里怎么带座位】

引擎收到 sync，会把每个业务的座位表一起打包，像这样：

{
  "apps": [
    {
      "id": "yys",
      "name": "阴阳师",
      "slots": {
        "business": {"seat": "A", "label": "阴阳师"},
        "bind":     {"seat": "B", "parent": "...", "shot": {...}, "click": {...}},
        "alert":    {"seat": "C", "label": "悬赏协助"},
        "switches": [
          {"seat": "1", "label": "全局处理", "default": true},
          {"seat": "2", "label": "掉线",     "default": false}
        ]
      },
      "tasks": [
        {"task_id": "collaboration", "name": "绘卷速刷", "stats": [...], "default_count": "inf"},
        ...
      ]
    }
  ],
  "devices": [
    {
      "device": "MuMu安卓设备",
      "target_title": "MuMu安卓设备",
      "adapter_key": "MuMu安卓设备",
      "app_id": "yys",
      "state": "idle",
      "detail": "",
      "running_task_id": "",
      "window_w": 733,
      "window_h": 412
    }
  ]
}


【座位信号：业务 → UI】

业务说"我给提醒位发个信号"，引擎转成：

事件名：slot_changed
数据：{
  "device": "MuMu安卓设备",
  "slot": "alert",         ← 座位名（business / bind / alert / switch_1 / switch_2 ...）
  "value": "blink"         ← 值（各座位自己解释）
}

- 提醒位（alert）：value = "blink" 就闪，"off" 就停
- 开关位（switch_1、switch_2...）：value = true / false
- 业务位（business）：一般不用，UI 从 sync 里拿就行


【座位信号：UI → 业务】

用户在 UI 上操作某个座位，UI 发给引擎：

命令名：slot_command
数据：{
  "device": "MuMu安卓设备",
  "slot": "switch_1",      ← 哪个座位
  "value": false           ← 用户操作后的值
  activate_window:
      {"device": "<设备标识>"}
}

引擎收到 → 转给对应业务的老板 → 老板自己知道 switch_1 是啥意思
"""