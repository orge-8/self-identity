# self-identity 自我身份档案

为 Bot 提供结构化的「我是谁」中枢：档案检索、人设图库与原图、QQ 头像，
外加声明校验与视觉比对两件防漂移工具。

主配置的 `[personality]` 管**每轮都在的人设与行为风格**，本插件管**结构化身份档案 / 图库 / 自我信息检索 / 自检**——
二者互补且不重复：插件**运行时完全被动**，不改写任何模型请求；`/人设卡片` 把档案渲染成文本，
由你决定要不要粘进宿主配置（见「与宿主 [personality] 的分工」）。

- 插件 ID：`org.mai-mai.self-identity`
- 基于 [SengokuCola/self_identity_plugin](https://github.com/SengokuCola/self_identity_plugin)（v1.3.1，MIT）的思路重写，修复其易用性窟窿（配置热重载不生效、ID 与路径耦合、无模糊匹配等）。

## 功能

| 能力 | 类型 | 说明 |
| --- | --- | --- |
| `search_self_information` | Tool | 检索自我档案：打分阈值 + 多关键词 AND + 空条目守卫 |
| `view_all_image` | Tool | 分页浏览人设图库缩略图（PIL 512px 惰性生成；页码越界自动夹取并提示；显示装扮标签） |
| `get_self_image` | Tool | 获取人设原图：**装扮标签 tag** / 序号 / 精确名 / ID / 模糊名 / `random` |
| `get_self_avatar` | Tool | 读 `bot.qq_account` → qlogo.cn 高清头像，httpx 异步 + 24h 磁盘缓存 |
| `verify_self_claim` | Tool | 校验一句关于自己的说法 → `符合 / 冲突 / 档案无记录` + 依据条目（防人设漂移） |
| `compare_with_self_image` | Tool | VLM 比对：待判断图片 ↔ 人设参考图（可按装扮标签选参考图；服装不同不算冲突） |
| `/人设状态` | Command | 条目数 / 图库数 / **装扮标签 / 基准参考图** / 档案卡长度 / 视觉生效值 |
| `/人设卡片`（别名 `/身份卡片`、`/人设卡`） | Command | 输出**可直接粘贴**到宿主配置的身份档案卡 |
| `/人设刷新` | Command | 重扫图库 + 重载配置（热生效，无需重启） |
| `/人设图库` | Command | 文本图库清单（带标签，给人看） |

图片 ID 为**纯内容哈希**（sha1 前 12 位）：改名/挪目录不换 ID，改内容才换。

## 安装

1. 把整个 `self-identity/` 目录放进 MaiBot 的 `plugins/` 目录（替换升级：**整目录替换**，并按需删除旧 `config.toml`）。
2. **完整重启 MaiBot**（新组件与 capabilities 只在启动时注册，热重载不生效）。
3. 启动后 Runner 会自动生成 `config.toml`；在 WebUI「插件管理」里确认插件已加载，并填写 `[profile]` 与 `infos`。
4. 想让身份**每轮都生效**：执行 `/人设卡片`，把输出粘进 `bot_config.toml` 的 `[personality]`（见下节）。

依赖：仅标准库 + `maibot_sdk`；`Pillow`（缩略图）与 `httpx`（头像/图片下载）缺失时对应功能自动降级，插件仍可加载。

## 配置

```toml
[plugin]
enabled = true
config_version = "1.3.0"
debug = false

[identity_image]
image_dir = "self_image"        # 人设原图目录（相对插件目录或绝对路径）
thumbnail_dir = "image_thumbup" # 缩略图缓存目录（可整目录删除，按需重建）
thumbnail_max_px = 512
page_size = 10
default_reference_tag = "默认"   # 带该标签的图 = 基准参考图（多图时无参调用默认用它）

[search]
default_limit = 5
match_threshold = 15.0          # 命中阈值：调低更宽松，调高更严格

[profile]                        # 结构化身份档案（身份卡素材，默认空模板）
name = "小镜"
role = "一家 24 小时便利店的夜班店员（门店门面，熟客都认识她）"
appearance = "黑色齐肩短发、棕色瞳孔；藏青色店服围裙，左胸口别着名牌，右腕戴便利店发的电子表"
personality = "随和爱聊天，记性很好；听到熟悉的歌会不自觉哼出声，深夜没什么客人时喜欢整理货架"
speech_style = "口语短句为主，偶尔蹦出收银台口头禅（「要袋子吗」「会员码看一下」）"
taboos = "询问年龄"
background = "大学在读，为了攒学费在便利店打工；自称「深夜货架的值班员」"
max_card_chars = 400             # /人设卡片 输出长度上限（v1.3.0 从 [injection] 迁来）

[general]                        # 自我信息（配置页里叫「自我信息」节）
# ⚠ 必须放在 [general] 节里，不能写回根级 [[infos]]（原因见「自我信息为什么在 [general] 节」）
[[general.infos]]
title = "名字"
keywords = ["名字", "称呼", "怎么叫"]
full_information = "小镜，店里的同事和熟客都这么叫她。"

[[general.infos]]
title = "喜欢的食物"
keywords = ["食物", "喜好", "吃"]
full_information = "最喜欢临期饭团打折的那个点；认为关东煮要用海带汤底才正宗。"

[[general.infos]]
title = "身份与工作"
keywords = ["身份", "工作", "职业", "做什么", "便利店"]
full_information = "24 小时便利店的夜班店员，负责补货、收银，以及把临期食品摆上打折区。"

[[general.infos]]
title = "形象细节"
keywords = ["形象", "外貌", "发型", "制服"]
full_information = "黑色齐肩短发、棕色瞳孔，藏青色店服围裙，左胸别名牌，右腕戴一只便利店发的电子表。"

[vision]                         # 视觉比对（v1.1.0）
enabled = true
task_name = "vlm"                # Host 模型任务名（不是具体模型名）
model_name = ""                  # 具体模型名，留空用任务默认
max_tokens = 1600                # 低于 1600 会被运行期抬到 1600（思考 token 也占额度）
temperature = 0.0
ignore_outfit_differences = true  # 不同服饰/画风不算冲突：只按发色/发型/瞳色/五官/气质判定
image_timeout_seconds = 90.0     # 单图预算；会被外层预算（消息×80% / RPC−10s）夹取，生效值见 /人设状态
message_timeout_seconds = 110.0  # 整条消息总预算（必须大于单图预算）
```

### 示例档案：小镜（便利店夜班店员，通用模板）

上面 `[profile]` 与 `[[general.infos]]` 是一整套**可直接粘贴**的通用模板示例（完全虚构的便利店夜班店员），
换成你自己的设定时注意素材的权威顺序：

1. 你为 bot 写的设定文档——姓名、外貌、背景都以它为准；
2. 你希望 bot 的说话风格——写在 `speech_style` **或**宿主 `reply_style`（二选一，见下）；
3. 素材里没有的细节**不要凭想象补**——档案里写了假设定，`verify_self_claim` 会把它当成事实来源。

> v1.3.2 起**本包不附带任何人设图与设定页**（此前随包的示例图已移除，避免二创版权与隐私问题）；
> 把你自己的图放进 `self_image/` 即可，命名规则见下。

#### 自我信息为什么在 `[general]` 节（v1.3.1 修复的静默丢数据缺陷）

v1.3.0 之前 `infos` 是**根级字段**（`[[infos]]`），看起来更自然，但真机上「配置页加了条目、`/人设状态` 永远 0 条」。根因是三方契约不合：

| 环节 | 行为 |
| --- | --- |
| 宿主配置页 | 按「**节名 = 点号路径**」读写（`dashboard/src/routes/plugin-config/utils.ts` 的 `getNestedRecord` / `setNestedField`） |
| SDK Schema | 把**根级字段**归入一个**合成的 `general` 节**（`maibot_sdk/config.py::generate_plugin_config_schema`） |
| 根配置模型 | `PluginConfigBase` 是 `extra="ignore"` ⇒ 收到的 `{"general": {"infos": [...]}}` 整节被丢掉 |

结果：配置页读到的是 `config.general.infos`（永远 undefined，**已有的条目在界面上也看不见**），
保存时把值写进 `general` 节，再被根模型吞掉 —— 用户怎么填都是 0 条。

修法是把 `infos` 声明进一个**真实存在的 `general` 节**（节名与宿主约定的落点一致），读写两侧就对上了。
同时保留向后兼容：旧 `config.toml` 的根级 `[[infos]]` 仍可用，
插件在归一化时会把它迁进 `[general]`（两处都有值时以 `[general]` 为准并打 WARNING，绝不静默覆盖）。

> 升级到 v1.3.1 时**故意没有 bump `config_version`**：Runner 的版本重建语义是
> 「新默认骨架 + 旧值覆盖」，且**只覆盖新骨架里存在的键** —— 一旦 bump，
> 旧文件里的根级 `[[infos]]` 会在归一化之前就被丢掉（插件连抢救的机会都没有）。
> 保持 `1.3.0` 则 `general` 节由「补齐缺失字段」自动出现，同时根级条目还能被迁移。

要点：

- `name` 必填——`verify_self_claim` 的名字冲突判定只依赖它；
- `taboos` 用逗号分隔多个忌讳词，命中即判「冲突」（示例模板里是「询问年龄」）；
- `appearance` 写得具体（发色/瞳色/服装/配饰），视觉比对的参考提示会带上它；
- `speech_style` 建议**只写一套**：要么写在这里（用 `/人设卡片` 拷进宿主 `[personality] reply_style`），
  要么直接写在宿主 `reply_style` 里——两边都写风格就是两份措辞，模型可能同时执行两套约束；
- 素材里没写的细节（惯用手、口头禅清单、服装备选等）**不要凭想象补**——档案里写了假设定，`verify_self_claim` 会把它当成事实来源；
- 带文字排版的设定页**别放进图库**——画面里的文字会被视觉模型当噪声，影响比对质量；
  这类图放在插件目录外的文档位置即可，`image_dir` 只放要给模型看的图。
- 图库多于一张时，无参调用取**带「默认」标签**的那张；没有标记「默认」的图且多于一张时，
  必须显式给 `tag` / `image_index` / `image_name`（可传 `random`）——这是刻意设计：宁可要求指明，也不要拿错参考图。

### 与宿主 `[personality]` 的分工（v1.3.0：改由宿主承载，插件不再注入）

宿主自己就把人设每轮填进两个模型请求里（`prompts/<语言>/` 下的模板占位符）：

| 占位符 | 填充源 | 谁在看 | 作用 |
| --- | --- | --- | --- |
| `{identity}` | `[personality] personality` | replyer（写回复文本的模型） | 「你是谁」（模板里还会自动加「你的名字是{nickname}」） |
| `{reply_style}` | `[personality] reply_style` | replyer | 说话风格 |
| `{behavior_style}` | `[personality] behavior_style` | planner（决定做什么、能调工具的模型） | 行为风格 |
| 超短机制 | `[personality] multiple_reply_style` | replyer | 概率性极短回复 |

**v1.1.0–v1.2.0 里本插件用 `maisaka.replyer.before_model_request` hook 主动注入身份卡，v1.3.0 已移除**，
原因是它落在了全系统最冗余的位置（结论来自 MaiBot-1.3.2 宿主源码核实）：

1. **紧邻 `{identity}`**：注入的 SystemMessageItem 就加在 replyer 请求里，而同一个请求头部已经有
   `[personality] personality` —— 写上「我是谁」就是同一诉求两份措辞，风格约束还可能互相打架；
2. **提示不可执行**：replyer 那条 hook 的载荷里**没有** `tool_definitions`（planner 的才有），
   所以卡里「需要时调用 `search_self_information` 查询」是一句模型做不到的提示，可能换来一句「我查一下」+ 编造；
3. **只活一次请求**：宿主**不持久化** hook 注入项（下一轮是重新注入，不是命中判重），
   于是同一会话会出现「首轮知道、第二轮就忘」的前后不一致；而 `{identity}` 是每轮都在的；
4. **代价常驻**：BLOCKING hook 挂在回复主链的每一次 attempt 上，长期依赖宿主的内部快照契约
   （`SystemMessageItem` / `item_schema_version`）；而收益只是「首轮可能更准一点」——
   模型本来就有 `search_self_information` 工具，planner 的工具列表里带着它的触发条件说明。

所以现在的关系是：**插件生成文本、宿主承载身份**，结构上不存在重复，也不碰回复主链。

#### 想让身份「每轮都生效」：用 `/人设卡片`

```
/人设卡片
```

输出形如：

```
🪞 身份档案卡（复制下面分隔线之间的内容）
用途：粘进宿主 bot_config.toml 的 [personality]，每轮都会生效：
· 想让它影响「怎么说话」→ 填 personality（replyer 的 {identity}）
· 想让它影响「怎么决策」→ 填 behavior_style（planner 的 {behavior_style}，planner 能调工具）
本插件不再主动注入，避免与宿主 personality 出现同一诉求的两套措辞。
──────────
我是小镜。
身份定位：…
外貌：…
另有 4 条自我信息（名字、喜欢的食物、…），细节需要时调用 search_self_information 查询。
──────────
```

推荐分工：

- **宿主 `[personality]`**：`personality` 写「是谁 + 性格」、`reply_style` 写说话风格、`behavior_style` 写行为准则；
- **本插件**：结构化细节（生日/人际关系/来历…）、人设图库、`verify_self_claim` 自检、`compare_with_self_image` 视觉比对；
- 卡片里那句「另有 N 条自我信息…调用 search_self_information」**只在你把它粘进 `behavior_style` 时才可执行**
  （planner 有工具）；粘进 `personality` 时建议删掉这句，或改成陈述句。

> 升级提醒：`config.toml` 首跑落盘后不再跟随插件更新，**换版本时请删掉旧 `config.toml` 重新生成**，
> 并回填 `profile` / `infos`。v1.3.0 删除了 `[injection]` 节、把 `max_card_chars` 移到 `[profile]`，
> 配置版本号同步 bump —— Runner 按「新默认为骨架 + 旧值覆盖」重建，
> 残留的 `[injection]` 会被配置模型按 `extra="ignore"` 静默忽略（不会报错）。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/人设状态`（别名 `/自我状态`） | 档案/图库/档案卡长度/视觉**生效值**总览（配置与生效值不一致时会标注「已被夹取」「已抬升」） |
| `/人设卡片`（别名 `/身份卡片`、`/人设卡`） | 输出可粘贴到宿主 `[personality]` 的身份档案卡 |
| `/人设刷新`（别名 `/刷新人设`） | 重扫图库 + 重载配置 |
| `/人设图库`（别名 `/图库清单`） | 文本图库清单 |

## 人设图与多套装扮（不同服饰 / 画风）

### 命名即标签

图片放进 `self_image/`（或改 `image_dir`），**文件名里 `#` 之后是标签**：

```
小镜#默认,制服,立绘.jpg    ← 带「默认」标签 = 基准参考图（多图时无参调用默认用它）
小镜#夏日,海边.jpg
小镜#女仆.jpg
小镜#头像.jpg
```

- 标签用 `,`／`、`／空格分隔；`#` 之前的部分是基础名（模糊匹配也认基础名）；
- 标签用于按服饰/风格**选参考图**，多个标签是 **AND** 语义（`tag="夏日,海边"`）；
- 改文件名不动图片 ID（ID 取内容哈希），所以随时可以重命名打标签；

### 后续加图：三步

**不用改代码、不用改配置、不用重启**（加图不涉及 `_manifest.json` / capabilities）。

1. **放文件**：拷进 `self_image/`（或你在 `[identity_image] image_dir` 里指定的目录）。
   支持 `.jpg .jpeg .png .webp .gif .bmp`。
2. **改名**：`基础名#标签1,标签2.扩展名`，例如新加一套演出服就叫 `小镜#演出,舞台.jpg`。
3. **刷新并验证**：

   ```
   /人设刷新      ← 更新 /人设状态 里的图库计数与标签行
   /人设图库      ← 文本清单，应看到「标签：演出、舞台」
   ```

   之后用户说「穿演出服的你」→ 模型走 `get_self_image(tag="演出")`；
   说「这是你吗」+ 图片 → `compare_with_self_image` 仍默认用那张带 `默认` 的立绘（新图不会抢默认位）。

**生效时机**：`get_self_image` / `view_all_image` / `compare_with_self_image` **每次调用都实时重扫目录**，
新图放进去立即可用，连 `/人设刷新` 都不用等；但 `/人设状态` 显示的是**上次刷新的快照**，
所以看计数要发 `/人设刷新`。缩略图是首次扫描时按需生成的（`image_thumbup/`，可整体删除重建）。

#### 命名规则

| 规则 | 说明 |
| --- | --- |
| `#` | 分割「基础名」与「标签」；**基础名里不能再出现 `#`**（只按第一个 `#` 切） |
| 标签分隔符 | `,` `，` `、` `;` `；` `/` 空格 都可以；重复标签自动去重 |
| 没有 `#` | 视为无标签图，只能按文件名 / 序号选它 |
| `默认` | 带该标签的那张 = **基准参考图**（多图时无参调用默认用它）；**只给一张加**（可在 `default_reference_tag` 改标签名） |
| 大小写 | 匹配不敏感 |

#### 五个坑

1. **别用网盘 / QQ / FTP 直接传带 `#` 的文件名** —— 会被截断或改名，标签随之丢失
   （图还能用，只是退回按序号/文件名选）。**打包 zip 再解压**最稳。
2. **`默认` 标签只能给一张** —— 两张都加不会报错，会静默取文件名排序靠前的那张。
   想换基准参考图：去掉旧那张的 `默认` 标签，或改 `default_reference_tag`。
3. **带文字排版的设定页别放图库** —— 画面里的文字会被视觉模型当噪声、拉低比对质量；
   这类图放在插件目录之外的文档位置即可。
4. **别在对话里记「第几张」** —— `image_index` 按文件名排序，加一张新图就让旧序号漂移；
   让模型优先用**标签**（工具描述也是这么引导的）。
5. **同名标签命中多张不会瞎猜** —— 会返回「匹配到多张（A、B），请再加一个标签或改用序号」。
   所以标签要能唯一定位，比如 `演出,舞台` 而不是只写 `演出`。

#### 尺寸与 ID

- **ID = 内容哈希**（sha1 前 12 位）：改名/挪目录 ID 不变；**改了内容才换 ID**，并会重新生成缩略图
  （旧缩略图会留在 `image_thumbup/` 里当垃圾，不影响正确性，可随时整目录删除）。
- 原图会直接进模型上下文：长边 1500～2400 是已验证可用的量级，再大建议先压；
  单张超过 15MB 的原图会被拒绝读取（v1.3.3 体积上限）。
- 图库是「**同一个 bot 的多套装扮 / 画风**」模型：不用于放第二个人物当另一个身份（persona packs 不在支持范围）。

### 模型怎么用

1. `view_all_image` 返回带标签的清单（含 `tags`、`is_default`），模型据此挑图；
2. `get_self_image(tag="女仆")` / `(image_name="random")` / `(image_index=2)` 取原图；
3. `compare_with_self_image(reference_tag="夏日")` 指定用哪套装扮当参考，
   不带参数就用「默认」那张；返回里会写明 `reference_image`、`reference_tags`；
4. 用户直接说「看看你自己」→ 模型走 `view_all_image`；说「穿女仆装的你」→ 走 `get_self_image(tag="女仆")`。

### 不同服饰不会被判成「不是同一个人」

这是多套装扮场景最容易踩的坑：**同一个角色换套衣服，视觉模型很容易判「冲突」**。所以：

- 比对提示词里显式写入**参考图的装扮标签**（「参考图的装扮／风格标签：默认、制服」）；
- 默认开启 `vision.ignore_outfit_differences`：判定依据限定为**发色／发型（含呆毛、发饰）／瞳色／五官／气质**，
  服装、配饰、场景、画风的差异写进 `differences` 而**不作为否定依据**；
- 只有角色本身特征不一致时才判 `冲突`；关掉该开关则服装也参与判定（适合「必须和参考图同一套装扮」的场景）。

## 视觉比对与声明校验

- 「这是你吗 / 画得像不像你」→ `compare_with_self_image`：默认取当前会话最近的图片（取不到时可用 `image_url` / `image_base64` 显式传图），与人设参考图比对后给出 verdict；可按 `reference_tag` 指定装扮。
- 「你不是长头发吗」→ `verify_self_claim`：先查档案再回答，输出 `符合 / 冲突 / 档案无记录` 与依据条目。
- 视觉调用会显式带 RPC 超时（Host `cap.call` 默认只有 30s）；`max_tokens` 低于 1600 会被运行期抬升，`/人设状态` 里能看到生效值。

## 故障排查

| 现象 | 处置 |
| --- | --- |
| 插件没加载、日志里本插件零输出 | 大概率是旧 `config.toml` 版本非法：删掉插件目录里的 `config.toml` 让 Runner 重新生成，再到 WebUI 回填内容 |
| 改了配置没生效 | WebUI 保存会热生效；直接改文件后发 `/人设刷新`；改了 `_manifest.json`/capabilities 才需要完整重启 |
| 身份卡没进模型上下文 | v1.3.0 起插件**不再注入**：执行 `/人设卡片` 把输出粘进宿主 `bot_config.toml` 的 `[personality]`（`personality` 影响语气、`behavior_style` 影响决策） |
| 配置页加的自我信息条目保存后消失（`/人设状态` 仍「0 条」） | v1.3.0 的已知缺陷：配置页把 `infos` 读写成 `config.general.infos`，而根模型 `extra="ignore"` 把这一整节丢掉 ⇒ **界面里加的条目保存即丢失**。已验证：v1.3.0 下手写根级 `[[infos]]` 仍然有效，且不会被配置页保存覆盖（只有界面路径坏）。**v1.3.1 已修**：升级后在配置页添加即可，旧根级写法会自动迁入 `[general]` |
| `/人设卡片` 说「卡片是空的」 | `[profile]` 全空且 `infos` 为空时没有可渲染内容；填任一项后重试（`/人设状态` 也能看到卡片长度） |
| 卡片被截断 | `[profile] max_card_chars` 默认 400，调大即可（上限 1200）；粘进宿主配置时按需保留重点 |
| 身份卡改动后模型行为没变 | 卡片只是**素材**：粘进宿主配置才生效；改完宿主配置后发 `/人设刷新`（插件侧）并让宿主重载 `bot_config.toml` |
| 工具说「人设图库为空」 | 确认图片在 `image_dir` 指向的目录、扩展名受支持（`.jpg .jpeg .png .webp .gif .bmp`） |
| 加了新图但清单/计数没变 | 工具调用会实时重扫，列表没变说明文件没进对目录或被改名；`/人设状态` 是快照，发 `/人设刷新` 再看 |
| 新图的标签不见了 | 文件名里的 `#`、`,` 在传输途中被截断/替换了（网盘、QQ 直传、FTP 常见）；用 zip 传，或按 `基础名#标签.jpg` 重新命名 |
| `tag=` 报「匹配到多张」 | 标签不够唯一：加一个标签（`演出,舞台`）或改用完整文件名；图库不会替你猜 |
| 基准参考图不是想要的那张 | 检查是否有多张带 `默认` 标签（会静默取排序靠前的那张）；只保留一张，或改 `default_reference_tag` |
| 缩略图一直不生成 | 运行环境缺 Pillow：`pip install Pillow` |
| 头像获取失败 | 确认主配置 `bot.qq_account` 是有效 QQ 号；看日志 `get_self_avatar` 行 |
| 比对总是「没结果」 | 看工具返回的失败分类：**截断** → 调大 `vision.max_tokens`（或让任务绑一个非推理模型）；**没有按 JSON 回答** → 换视觉模型或调 `temperature`；**视觉请求超时** → 瓶颈通常在 Host 的 Provider timeout（`[llm].timeout_seconds`），插件侧调大 `image_timeout_seconds` 只是让图片多占一会儿 |
| 检索永远「没有匹配」 | 降低 `search.match_threshold`（默认 15.0），或给条目补关键词 |

## 防护点（v1.3.0 全检结论，v1.3.2 / v1.3.3 增补）

| 风险 | 防护实现 | 复现用例 |
| --- | --- | --- |
| SSRF（图片 URL 来自消息/模型参数） | `resolve-then-check`：先 DNS 解析再逐个校验 IP（含 IPv4-mapped IPv6、组播、link-local、回环）；**手动逐跳重定向**，每一跳重新校验后才发包 | `tests/test_audit.py::test_redirect_to_internal_host_is_never_requested`（断言**出站记录**里没有内网请求）+ 反向验证用例 |
| 加固反成故障（白名单拒绝正常功能） | 只校验「能不能取」，不给下载路径加凭据；公网→公网重定向有正例对照 | `test_redirect_between_public_hosts_is_allowed` |
| 日志泄露 URL 里的签名/凭据 | `_safe_url()` 只保留 `scheme+host+path`，query 一律丢弃 | `test_log_url_is_redacted` |
| 同步阻塞事件循环（PIL 缩略图 / 读图 / base64） | 图库扫描、图片读写、编解码一律 `asyncio.to_thread` | `test_gallery_scan_runs_off_the_event_loop`（ticker 计数 + 执行线程双判据，并带同步直调对照 ticker==0） |
| 提示注入（声明/补充描述拼进 prompt） | `neutralize()` 把连续尖括号硬打散；视觉 prompt 的 JSON 示例只承担一种语义 | `test_profile_hint_neutralizes_untrusted_text`、`test_compare_prompt_has_single_meaning_for_json_block` |
| 假冲突（把描述句当自称名字） | 只用显式命名句式（「我叫 / 我的名字是」），并做名字形态校验；**假冲突比漏报更糟** | `test_extract_claim_name_blocks_non_names`、`test_verify_claim_descriptive_sentence_is_not_conflict` |
| 静默失败（视觉输出不可解析） | 解析失败分四类（空内容 / 跑偏 / 截断 / 结构不合法）并**留住原始输出**；`max_tokens` 低于下限运行期抬升且 `/人设状态` 外显 | `test_diagnose_unparsable_three_classes`、`test_vision_max_tokens_floor` |
| 两个超时顺序反了（用户只看到「超预算」） | 运行期夹取 `min(单图, 消息×0.8)`，生效值与「已被夹取」标注在 `/人设状态` | `test_effective_timeout_short_fires_first` |
| 内层视觉预算越过 RPC 层（RPC 先超时、真因被文本启发式猜） | 生效超时再按 RPC 预算 −10s 兜底夹取（v1.3.2） | `test_effective_image_timeout_respects_rpc_budget` |
| 比对判定词的否定变体被静默判反（「不符合」被判「符合」） | 否定变体**先于**肯定词匹配；结构化 `same_person` 布尔优先于判定词，矛盾时对齐 verdict 并写进 differences 留痕（v1.3.2） | `test_normalize_verdict_negative_variants_are_conflict`（含旧实现反向验证）、`test_compare_images_structured_boolean_wins_and_annotates_contradiction` |
| 本地路径图片引用读到任意文件（非图片被当图喂给模型） | `file`/`path` 引用严格魔数校验 + 15MB 上限 + 读盘走线程池（v1.3.2）；不设目录白名单是为了不误杀宿主附件路径。**权限边界（审核建议②）**：通过校验的引用等价于「读进程权限内任意 ≤15MB 真图片」——证件照、截图等敏感图片请勿放在 bot 进程可访问的位置 | `test_local_image_reference_rejects_non_image_and_oversize` |
| 巨图原图无上限，整份 base64 进模型上下文 | 原图读取 stat 预检 + 15MB 上限（stat 与读取间替换有兜底复检），超限返回可读压缩提示（v1.3.3，插件中心审核建议①） | `test_original_image_size_cap` |
| 空条目日志序号失真（连续空条目重复「第 1 条」） | 按原始序号打日志（v1.3.2） | `test_collect_infos_blank_log_uses_raw_index` |
| 把插件永久挂在回复主链上（改写请求失败面 + 与宿主 `{identity}` 重复） | v1.3.0 移除注入：**零 hook、零请求改写**，身份改由宿主配置承载；源码级守门用例禁止 `HookHandler`/`modified_kwargs` 复活 | `test_no_request_rewriting_code_path`、`test_plugin_never_registers_request_rewriting_components`、`test_no_hook_component_static` |
| 配置页保存被静默吞掉（根级字段 ↔ 合成 `general` 节错位） | v1.3.1：`infos` 落在真实 `[general]` 节，读写两侧对齐；旧根级 `[[infos]]` 自动迁移、非列表值打 WARNING，绝不静默丢 | `test_webui_save_keeps_infos_in_general_section`、`test_legacy_root_infos_still_works`、`test_general_section_wins_over_legacy_root`、`test_webui_schema_exposes_infos_as_real_general_section` |
| 硬编码凭据 / 绝对路径 | 发布文件中零命中（脚本自检） | `test_no_hardcoded_credentials_or_absolute_paths` |
| 多套装扮被误判成「不是同一个人」 | 参考图装扮标签写进提示词 + `ignore_outfit_differences` 默认开启（服装差异只进 differences） | `test_compare_prompt_tolerates_outfit_differences`、`test_compare_images_passes_reference_tags_into_prompt`、`test_plugin_compare_uses_default_reference_and_reports_tags` |
| 多图时拿错参考图 / 无法选图 | 文件名标签（AND 语义）+ 「默认」基准参考图；未标记且多图时给可读错误并列出可用标签 | `test_resolve_image_by_tag_and_default`、`test_resolve_image_without_default_asks_for_tag`、`test_plugin_gallery_tag_flow` |

> **版本号说明**：v1.1.2 新增了 `injection.dedupe_with_host_personality` 字段，因此
> `_manifest.json` 的 version 与配置模型的 `plugin.config_version` **同步**为 `1.1.2`；
> Runner 的 rebuild 语义是「新默认值为骨架 + 旧值覆盖」，所以这是**保值**升级，无需手工重建配置。
> v1.1.3 只改文档与示例（按官方设定页更正外貌、随包附带设定页参考图），manifest 版本号递进，
> **`config_version` 保持 `1.1.2`**——不改配置结构，不触发配置重建。
> v1.1.4 把图库主参考图换成无文字全身立绘，设定页移入 `docs/` 只作文档，同样不动配置结构。
> v1.2.0 新增多套装扮支持（文件名标签 + 基准参考图 + 装扮无关判定），
> 因此 `_manifest.json` 与 `config_version` **同步为 `1.2.0`**。
> v1.3.0 **移除主动注入**（删除 `[injection]` 节、hook 与 `inject_si.py`），新增 `/人设卡片`，
> `max_card_chars` 迁入 `[profile]`；`_manifest.json` 与 `config_version` **同步为 `1.3.0`**——
> 同为保值重建，残留的 `[injection]` 节按 `extra="ignore"` 静默忽略，**无需手工改配置**。
> v1.3.1 修掉「配置页加的自我信息保存后消失」：`infos` 从根级字段改为真实的 `[general]` 节
> （根因与迁移见「自我信息为什么在 `[general]` 节」）。**`config_version` 故意保持 `1.3.0`**——
> 一旦 bump，Runner 的「新骨架 + 只覆盖骨架里存在的键」重建会把旧文件的根级 `[[infos]]`
> 在插件归一化之前丢掉（连迁移的机会都没有）；保持版本号则 `[general]` 由补齐缺失字段出现，
> 根级条目还能被自动迁移。**无需手工改配置。**
> 同版追加「后续加图：三步」与图库相关排查项（纯文档补充，无行为变化）——
> **`_manifest.json` 版本保持 `1.3.1`**，避免真机与包之间出现无意义的版本漂移。
> v1.3.2 全检修复（无配置结构变化，**`config_version` 保持 `1.3.0`**，无需手工改配置）：
> ① `normalize_verdict` 的否定变体（不符合／不相同／不一致／not same／no match…）此前会被
>    肯定词的子串兜底**静默判反**成「符合」，现按「全等 → 否定 → 肯定 → 布尔」顺序归一；
>    且结构化 `same_person` 布尔优先于判定词，两者矛盾时对齐 verdict 并写进 differences 留痕；
> ② `file`/`path` 本地图片引用加**严格魔数校验**与 15MB 上限（只放行真图片，防任意本地文件
>    被当图读进模型请求），读盘改 `asyncio.to_thread`；刻意不设目录白名单，避免误杀宿主附件路径；
> ③ 生效单图超时按 RPC 预算（150s−10s）兜底夹取——配置上限 170s 理论上可越过 RPC 的 150s；
> ④ 头像缓存的磁盘读写移入线程池；空条目日志改报**原始序号**（此前连续空条目重复「第 1 条」）；
> ⑤ **随包人设示例全部移除**（此前的 4 张示例图与设定页、README 里的具体人设档案）：
>    示例图涉及二创版权且 README 本就声明「别二次分发」，公开分发改为只带通用模板示例，
>    `self_image/` 保留空目录（`.gitkeep`），放图即用。
> **`_manifest.json` 版本同步为 `1.3.2`**。
> v1.3.3（插件中心审核建议落地，无配置结构变化，**`config_version` 保持 `1.3.0`**）：
> ① `get_self_image` / 比对参考图的**原图读取加 15MB 体积上限**（stat 预检 + 读取后兜底复检），
>    超限返回可读压缩提示——此前原图侧无上限，多大就 base64 多大进模型上下文；
> ② README 明确 `file`/`path` 引用的**权限边界**：通过魔数校验的引用等价于「读进程权限内
>    任意 ≤15MB 真图片」，敏感图片请勿放在 bot 进程可访问的位置（刻意不设目录白名单，
>    避免误杀宿主附件路径）。
> **`_manifest.json` 版本同步为 `1.3.3`**。

> 已知残余（评估后接受，不修）：`resolve-then-check` 的校验与实际连接之间存在 DNS 重绑定
> （TOCTOU）窗口——httpx 无连接前钩子，属该方案的业界常规残余；图库每次工具调用全量重扫、
> 逐图读文件算内容哈希，是「新图放入立即可用」卖点的刻意取舍（大图库时可改为按
> `(path, mtime, size)` 缓存哈希）。

**本地环境覆盖不到、真机需另行确认的分支**（如实标注，v1.3.0 后已随注入一起消失的不再列出）：

- 真实视觉模型的输出结构与 `task_name`/`model_name` 解析（本地用打桩响应）；
- 缺 `Pillow` / 缺 `httpx` 的真机降级路径（本地两库都已安装，只走了正常路径）；
- 真机上 `/人设卡片` 输出被群聊/私聊通道的分段与长度限制影响时的观感（本地只验证了文本内容）；
- 真机 dashboard 里 `[general]` 节的**界面观感**：本地只按渲染端源码（`FieldRenderer` /
  `SectionRenderer` / `ListFieldEditor`）与 SDK Schema 断言了控件契约（对象列表 + label/hint），
  没有浏览器实测。

## 本地开发

在 devkit 根目录（含 `run_gates.py` 的那层）执行：

```bash
python run_gates.py plugins/self-identity                              # 静态检查 + FakeHost 冒烟
python -m pytest plugins/self-identity/tests/test_self_identity.py -q  # 行为回归（70 项）
python -m pytest plugins/self-identity/tests/test_audit.py -q          # 审计用例（22 项）
python plugins/self-identity/tests/smoke_test.py                       # 包式加载冒烟（52 项 + 真机探针）
```

> `run_gates.py` 属于 devkit，**不在插件目录里**，别在插件目录内直接执行它。

