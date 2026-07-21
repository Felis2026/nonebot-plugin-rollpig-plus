<div align="center">
  <img src="docs/assets/logo.jpeg" width="180" alt="rollpig-plus logo">

  <h1>🐖 rollpig-plus 🐖</h1>

  <p><strong>“今天是什么小猪”的增强维护版</strong></p>
  <p>支持云端资源同步、图片版小猪图鉴、EX Lv. 成长、AI 烤猪与多 Bot 状态同步。</p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python >= 3.10">
    <img src="https://img.shields.io/badge/NoneBot-2.4%2B-black" alt="NoneBot >= 2.4">
    <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT License">
    <img src="https://img.shields.io/badge/Version-0.9.0-ff69b4" alt="Version 0.9.0">
  </p>
</div>

> 本项目最初基于 [Bearlele/nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig) 修改，当前作为拓展分支继续开发。

## 🧭 和原项目怎么选

| 选择 | 更适合的情况 |
| --- | --- |
| 原作插件 | 想要更轻量、更接近最初玩法，只需要本地“今日小猪 / 随机小猪 / 找猪”等基础功能。 |
| rollpig-plus | 想要图片版小猪图鉴、EX Lv. 成长、多 Bot 状态同步、AI 烤猪、烤群友与日报等增强功能。 |

rollpig-plus 的目标是作为独立维护的增强分支继续演进：保留原作的核心趣味，拓展部分玩法，同时把资源、图鉴、云端同步和稳定性做得更工程化。原rollpig本体依然能与本项目一样获取到每月更新（也许）的最新小猪。

迁移时建议把原作插件替换为 rollpig-plus，不要在同一个 Bot 进程里同时加载两者；两者的基础指令和 `rollpig_*` 配置键高度重合，同时加载会造成命令响应和配置读取混杂。

## ✨ 效果预览

| 今日小猪 | 小猪图鉴 | 烤群友 |
| --- | --- | --- |
| <img src="docs/assets/preview-today.jpg" width="220" alt="今日小猪预览"> | <img src="docs/assets/preview-catalog.png" width="360" alt="小猪图鉴预览"> | <img src="docs/assets/preview-roast.jpg" width="220" alt="烤群友预览"> |

## 📦 安装

环境要求：Python `>=3.10`，NoneBot `>=2.4.0`。

推荐使用 `nb-cli` 安装：

```bash
nb plugin install nonebot-plugin-rollpig-plus
```

如需固定到指定版本：

```bash
pip install nonebot-plugin-rollpig-plus==0.9.0
```

手动安装后，请确认 NoneBot 已加载插件模块：

```python
nonebot.load_plugin("nonebot_plugin_rollpig_plus")
```

图片版图鉴已经完全改为纯 Pillow，插件不再依赖 HTML 模板、Playwright 或 Chromium。

## 🐷 指令一览

| 指令 | 说明 |
| --- | --- |
| `今日小猪` / `今天是什么小猪` | 抽取今天属于你的小猪。每个用户每天只会生成一次结果，重复查看不会改变。 |
| `随机小猪 [数量]` | 从 PigHub 随机获取猪猪图，最多 10 张。 |
| `找猪 关键词` / `搜猪 关键词` | 从 PigHub 搜索猪猪图，例如 `找猪 玩偶`。 |
| `明日小猪` | 预测明天的小猪运势。 |
| `昨日小猪` | 查看昨天抽到的小猪。 |
| `今日烤猪` | 把今天的小猪做成美食；AI 烤猪需额外开启并配置 Key。 |
| `烤群友 @目标` | 在群聊中烤一位群友，带充能、概率与目标状态限制。 |
| `我的猪圈` | 查看已解锁数量、收藏率、最高 EX Lv.、本命猪等摘要。 |
| `小猪图鉴 [页码]` | 生成图片版小猪图鉴。 |
| `本周小猪` | 生成本周猪猪总结长图。 |
| `小猪日报 状态` | 查看本群每日总结推送状态。 |
| `小猪日报 开启` / `小猪日报 关闭` | 群主/管理员控制本群每日总结推送；SUPERUSER可追加群号控制其他群。 |

### 抽取与成长

- 每个用户每天只能抽取一次，跨天后重新抽取。
- 重复抽到已解锁小猪会提升专家等级（EX Lv.）。
- 连续重复时，后续抽到新猪的概率会逐步提高。
- 特殊形态（人类、熟食、吃掉了、售罄等）会参与烤猪与保护逻辑判定。

### 烤群友规则

- 常规概率：成功 60% / 逃脱 30% / 反噬 10%。
- 普通烤群友默认最多储存 2 次，每 8 小时恢复 1 次。
- 常规模式下，目标需先抽过今日小猪，且不能是人类、熟食形态、吃掉了或猪售罄。
- 加急点火口令可在限制范围内触发特殊成功判定；不会绕过目标资格检查。

### 每日总结控制

- 每日总结默认关闭；群主/管理员可在群内发送 `小猪日报 开启` / `小猪日报 关闭` 控制本群。
- SUPERUSER可跨群控制，例如 `小猪日报 开启 123456789`、`小猪日报 关闭 123456789`。

## ⚙️ 配置方法

插件内置完整默认值：**完全不写 `.env`、不写 JSON 也能启动并使用基础功能**。

默认状态下：

- 本地存储启用，数据写入插件自己的 localstore 数据目录。
- AI 烤猪关闭；未配置 Key 时自动使用本地文案模板。
- 公有小猪资源同步开启；同步失败会回退旧缓存或内置资源。
- 官方 GIF 动态小猪 overlay 会随云端资源同步固定启用；PJsk、用户自建包等其它私有 overlay 需要手动追加。
- 图片版小猪图鉴开启，默认 PNG 输出。
- 每日总结定时任务默认关闭，可通过群内命令或配置主动开启。

配置优先级：

```text
.env / NoneBot 配置 > JSON 配置文件 > 插件默认值
```

推荐分工：

- JSON 配置文件：放非敏感、稳定参数。默认读取 Bot 运行目录下的 `rollpig_config.json`，也会读取 `config/rollpig.json`。
- `.env`：放 Token / Key / 私密覆盖项；如需自定义 JSON 路径，只在 `.env` 写 `ROLLPIG_CONFIG_FILE=/path/to/rollpig_config.json`。

下面用 `jsonc` 展示注释方便阅读；多数示例值按插件默认值填写。官方 GIF overlay 不需要配置，PJsk 与本地包示例用于展示如何追加更多私有资源。实际 `rollpig_config.json` 必须是合法 JSON，可直接参考仓库内的 `rollpig_config.example.json`。

```jsonc
{
  "rollpig": {
    // ================================ AI 烤猪 ================================ //
    "rollpig_ai_enabled": false,               // 是否启用 AI 烤猪；只填 Key 不会自动开启
    "rollpig_model": "deepseek-v4-flash",      // AI 模型名称，默认 DeepSeek V4 Flash 非思考模式
    "rollpig_ai_timeout": 20.0,                // 单次 AI 文案生成超时时间（秒），超时自动回退本地模板
    "rollpig_ai_concurrency": 4,               // AI 文案生成并发上限，避免多人同时烤猪时堆积请求
    "rollpig_ai_max_tokens": 4096,             // AI 单次响应 token 上限，防止异常长输出
    "rollpig_ai_output_max_chars": 240,        // AI 文案入库前最大字符数，避免过长文本撑爆消息
    "rollpig_roast_cooldown_hours": 8,         // 普通烤群友每恢复 1 次所需小时数
    "rollpig_roast_charge_max": 2,             // 普通烤群友最多可储存次数；加急/强制点火不消耗

    // ================================ 存储与云端 ================================ //
    "rollpig_storage_backend": "local",        // local=本地 JSON；cloud=rollpig-cloud 多 Bot 同步
    "rollpig_cloud_api_url": null,             // cloud 模式的 rollpig-cloud 地址；默认不配置
    "rollpig_cloud_timeout": 5.0,              // 请求 rollpig-cloud 的超时时间（秒）
    "rollpig_cloud_strict_mode": true,         // true=云端异常直接失败；false=读接口可安全兜底

    // ================================ 小猪资源包 ================================ //
    "rollpig_resource_sync_enabled": true,     // 是否自动同步云端资源包；失败会回退旧缓存/内置资源
    "rollpig_resource_manifest_url": "https://pig.felislab.cc/resources/rollpig/manifest.json", // 公有全量包
    "rollpig_resource_sync_interval_hours": 24, // 自动检查资源更新的间隔小时数
    "rollpig_resource_sync_timeout": 10.0,     // 下载 manifest / pig.json / 图片的超时时间（秒）
    "rollpig_resource_max_file_size": 10485760, // 单文件下载大小上限，默认 10 MiB
    "rollpig_private_resource_manifests": [       // 推荐写法：多个私有 overlay 按顺序叠加
      {
        "name": "pjsk",                         // 可选：追加 PJsk 私有包，不需要可删除这一项
        "manifest_url": "https://pig.felislab.cc/resources/rollpig-pjsk/manifest.json"
      },
      {
        "name": "my-pack",                     // 可选：用户自建私有包也可以填本地 manifest 路径
        "manifest_url": "D:/my-rollpig-pack/manifest.json"
      }
    ],
    "rollpig_private_resource_manifest_url": "", // 旧版单私有包字段，仍兼容；新部署建议使用上方列表

    // ================================ 定时日报 ================================ //
    "rollpig_daily_summary_enabled": false,    // 未被命令/外部控制器覆盖的群是否默认启用每日总结；默认关闭

    // ================================ 普通小猪卡片 ================================ //
    "rollpig_card_font_path": null,            // Pillow 卡片字体路径；不填时标题和正文都使用内置 Source Han Sans SC Medium

    // ================================ 图片版小猪图鉴 ================================ //
    "rollpig_catalog_enabled": true,           // 是否启用“小猪图鉴”图片命令；不替代“我的猪圈”
    "rollpig_catalog_render_concurrency": 2,   // 默认同时绘制 2 张；512MB 部署建议设为 1
    "rollpig_catalog_cache_seconds": 300,      // 同一状态指纹的图鉴结果缓存秒数，不会额外刷新 copies
    "rollpig_catalog_output_format": "png",   // 输出格式；默认 PNG
    "rollpig_catalog_scale_factor": 2.0        // 2x 渲染，提升文字和徽章清晰度
  }
}
```

建议留在 `.env` 的敏感项与路径覆盖：

```properties
# DeepSeek API Key；仅填写 Key 不会开启 AI，还需设置 ROLLPIG_AI_ENABLED=true
ROLLPIG_DEEPSEEK_KEY=sk-xxxxxxxxxxxxxxxx

# rollpig-cloud Bearer Token
ROLLPIG_CLOUD_TOKEN=replace-with-token

# 私有资源 Bearer Token；公开静态资源通常不需要
ROLLPIG_PRIVATE_RESOURCE_TOKEN=replace-with-token

# 可选：指定 JSON 配置文件位置
ROLLPIG_CONFIG_FILE=/path/to/rollpig_config.json
```

补充说明：

- 未开启 AI 或未配置 Key 时，会自动回退到本地文案模板。
- 未配置云端时，默认继续使用本地 `pig_data.json` 存储，不影响单 Bot 正常运行。
- 云同步可自行部署 [rollpig-cloud](https://github.com/Felis2026/rollpig-cloud)，也可以联系维护者申请接入现有 API。
- `ROLLPIG_STORAGE_BACKEND=cloud` 时，今日小猪、图鉴成长状态、普通烤群友充能、加急点火次数会在多 Bot 间同步。
- `ROLLPIG_CLOUD_STRICT_MODE=false` 只允许读接口使用安全兜底；关键写接口不会偷偷回退本地，避免多 Bot 数据脑裂。
- 用户私有资源 overlay 优先级高于官方 GIF、公有云端资源和插件内置资源；推荐用 `rollpig_private_resource_manifests` 配置多个用户私有包，旧的 `rollpig_private_resource_manifest_url` 仍兼容。
- 自建本地私有包的目录结构、manifest 生成与配置方式见 [rollpig-resources 自建本地私有包指南](https://github.com/Felis2026/rollpig-resources/blob/main/docs/local-private-pack-guide.md)。
- `rollpig_daily_summary_enabled=false` 是默认值，表示未单独设置的群默认不推日报；可用 `小猪日报 开启` 为单群开启，或设为 `true` 让未覆盖的群默认开启。
- 普通卡片由 Pillow 渲染，默认使用内置 Source Han Sans SC Medium；如需微软雅黑、韩文覆盖更好的字体或其它字形风格，可自行提供字体并配置 `rollpig_card_font_path`。
- SUPERUSER可发送 `同步小猪资源` / `刷新小猪图鉴` 手动触发资源同步。
- 图片版图鉴每页固定展示 38 只小猪，不提供配置项，避免和当前底图安全区错位。

## 🐖 自定义小猪

本体内置资源位于：

```text
nonebot_plugin_rollpig_plus/resource/
```

最小资源格式：

```json
[
  {
    "id": "pig",
    "name": "猪",
    "description": "普通小猪",
    "analysis": "你性格温和，喜欢简单的生活，容易满足。"
  }
]
```

规则说明：

- `pig.json` 维护基础小猪信息。
- `resource/image/<id>.png` 或 `resource/image/<id>.gif` 为对应图片，文件名需要和 `id` 一致；同 ID 同时存在时优先使用 GIF。
- `pig_rules.json` 维护熟食、特殊形态等规则，避免污染上游兼容的 `pig.json` 基础格式。
- 普通卡片使用内置 Source Han Sans SC Medium 渲染 CJK 文本，并使用 `pilmoji` 与内置 Google Noto Emoji 32px ZIP 离线渲染彩色 Emoji，不依赖运行时联网。
- PNG 与 GIF 均会在普通卡片中统一渲染为 240×240 头像区域；建议资源原图也按 240×240 入库，避免缩放裁切产生偏移。
- GIF 仅用于“今日小猪 / 烤猪 / 烤群友”等普通卡片动态展示；图片版图鉴固定取首帧缩略图，保持静态陈列。
- GIF 资源建议透明背景、循环播放、无文字水印，帧数控制在 10～60 帧；较长动画会在完整周期内均匀收敛到最多 60 帧并保留总时长。解码工作量超过 1600 万像素帧、源帧超过 600、文件异常或实际为单帧时会退回静态 PNG。
- 公有云端资源会缓存到 `data/localstore/nonebot_plugin_rollpig_plus/resources/active/`。
- 多私有 overlay 会分别缓存到 `data/localstore/nonebot_plugin_rollpig_plus/resources/private_overlays/<name>/active/`；旧单私有包字段仍沿用 `private_active/`，方便无损升级。

## 📁 项目结构

```text
nonebot_plugin_rollpig_plus/
├─ __init__.py              # 插件元数据与 handler 导入
├─ card_renderer.py         # 今日小猪 / 烤猪 / 烤群友等普通卡片 Pillow 渲染
├─ catalog_renderer.py      # 图鉴业务数据、结果缓存与并发编排
├─ catalog_pillow_renderer.py # 图片版小猪图鉴纯 Pillow 绘制
├─ config.py                # 配置模型与 JSON 配置合并
├─ data_manager.py          # 本地 JSON 存储实现
├─ helpers.py               # 命令共享工具与消息发送辅助
├─ jobs.py                  # 定时任务与日报流程
├─ pighub_service.py        # PigHub 搜索 / 随机图缓存与兜底
├─ resource_manager.py      # 云端资源同步、多私有 overlay 与本地缓存加载
├─ roast_manager.py         # AI 烤猪与文案库管理
├─ roll_flow.py             # 抽猪业务规则
├─ roast_flow.py            # 烤群友业务规则
├─ runtime.py               # 宿主适配 / 群开关 / 运行时工具
├─ texts.py                 # 文案模板与特殊形态文本
├─ handlers/                # NoneBot 指令注册与参数解析
├─ store/                   # local / cloud 存储适配
└─ resource/                # 内置小猪、字体、Emoji 与图鉴底图资源
```

## 🔗 相关项目

- 原作插件：[Bearlele/nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig)
- 小猪资源包：[Felis2026/rollpig-resources](https://github.com/Felis2026/rollpig-resources)
- 云端存储服务：[Felis2026/rollpig-cloud](https://github.com/Felis2026/rollpig-cloud)
- PigHub（搜猪功能支持）：[pighub.top](https://pighub.top/)

## 📋 最近更新

### v0.9.0 图片版图鉴纯 Pillow 重构

#### 🎨 图鉴绘制
- 图片版图鉴彻底移除 HTML 模板和 Playwright 截图，改为纯 Pillow 绘制；分页、等级、NEW / MAX 标记和统计信息保持兼容。
- 重做半透明毛玻璃卡片、立体等级胶囊及顶部数据布局，并针对中文字体加载和固定资源缓存进行优化。

#### ⚙️ 依赖与性能
- 移除 `nonebot-plugin-htmlrender` 依赖、浏览器页面池和缩略图磁盘缓存，默认部署不再为 RollPig 启动 Chromium。
- 图鉴继续使用结果缓存、同键请求合流和有界并发；默认同时绘制 2 张，512MB 部署建议调整为 1。

完整更新日志见 [CHANGELOG.md](CHANGELOG.md)。

## 📄 许可证与致谢

插件代码使用 [MIT License](LICENSE)。

本项目最初基于 [Bearlele/nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig) 修改，感谢原作者提供的创意与基础实现。内置初始文案和部分猪图继承自原作；后续扩展资源由维护者创作、整理或来自公开用户投稿渠道。资源包的详细来源、使用边界与贡献说明请以 [rollpig-resources](https://github.com/Felis2026/rollpig-resources) 为准。

普通卡片内置 [Source Han Sans SC Medium](https://github.com/adobe-fonts/source-han-sans) 作为默认 CJK 字体，并使用 [pilmoji](https://github.com/jay3332/pilmoji) 渲染彩色 Emoji；内置 Emoji 图形资源来自 [googlefonts/noto-emoji](https://github.com/googlefonts/noto-emoji)，第三方资源声明见 `THIRD_PARTY_NOTICES.md`。
