from nonebot import require
from nonebot.plugin import PluginMetadata

# 确保依赖插件先被 NoneBot 注册（必须在本地模块 import 之前）
# data_manager.py 在模块加载时会调用 store.get_plugin_data_file()；
# 定时任务也需要 apscheduler 提前完成插件注册，避免商店/静态审核误判。
require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")

# 本地模块（在 require() 之后 import）
# 导入 jobs 即注册定时任务与生命周期回调；这些 import 不能移动到 require() 之前。
from . import jobs as _rollpig_jobs  # noqa: E402, F401
from . import reservation_delivery as _reservation_delivery  # noqa: E402, F401
from .config import Config  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="今天是什么小猪（今日小猪）Plus",
    description="基于原版Rollpig的拓展分支，新增拓展烤群友、图鉴成长等多项玩法",
    usage="""
    🐷 基础指令：
    今日小猪 / 今天是什么小猪 - 抽取今天的命运之猪
    随机小猪 - 随机看一张猪图
    找猪 -  从 PigHub 模糊搜索猪猪图
    
    🔮 趣味指令：
    明日小猪 - 预测明天的猪猪运势
    昨日小猪 - 查看昨天抽到了什么
    今日烤猪 - 把今天的猪做成美食
    烤群友 - 用魔法烤箱把群友做成美味的烤猪
    加急生火 - 每日一次的强制成功烤猪
    烤箱补货 - 管理员发起续标识投票，为本群活跃玩家恢复普通烧烤次数
    
    📊 统计指令：
    我的猪圈 - 查看解锁进度
    小猪图鉴 - 生成图片版小猪图鉴
    本周小猪 - 生成本周猪猪总结长图
    小猪日报 开启/关闭/状态 - 控制本群每日总结推送
    """,
    type="application",
    homepage="https://github.com/Felis2026/nonebot-plugin-rollpig-plus",
    supported_adapters={"~onebot.v11"},
    config=Config,
)

# ================= 指令处理区域 =================
# handlers 的导入顺序就是 matcher 注册顺序。
from .handlers import roll as _roll_handlers  # noqa: E402, F401
from .handlers import roast as _roast_handlers  # noqa: E402, F401
from .handlers import refill as _refill_handlers  # noqa: E402, F401
from .handlers import collection as _collection_handlers  # noqa: E402, F401
from .handlers import control as _control_handlers  # noqa: E402, F401
