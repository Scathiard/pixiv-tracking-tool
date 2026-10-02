# B11 标签译名只读研究

日期：2026-10-03；基线 main / 4c00d30；版本 0.2.0 / 20261002.1。

用途：保留只读源码调查与纯解析函数合成复现；不读取令牌、配置、真实图库或真实账号，不改生产代码或三份主文档。不自动清理，主会话审查后决定归档。

## 已确认的源码事实

- `app/src/client.py:442` 把远端 tags 转成 name 字符串列表，丢弃 translated_name。
- `app/src/core/archive.py:181-189` 保存 WorkTag 原名及 urls / remote_availability，并未保存完整远端响应。
- `app/src/core/models.py:76` 的 source_metadata 是可承载新 JSON 键的既有字段；新增译名元数据不必新增 SQL 列。
- `app/src/core/library.py:145-163` 的作品视图仅输出标签原名字符串；详情与整理面板使用这些值。
- `app/frontend/src/types.ts:33` tags 是 string[]；`App.vue:443,458` 展示原名；标签筛选候选见 `FilterPanel.vue:21`。
- 旧库导入 `core/legacy_import.py:153-168` 的 legacy 元数据来自旧字符串标签，不包含完整 App 响应。未检查任何真实数据库，因此不能保证每个真实条目都没有额外人工资料；现行数据链不产生译名。

## 一手来源与证据等级

- Pixiv 官方标签翻译发布说明（历史来源）：https://www.pixiv.co.jp/news/information/article/6192/ 。说明作品/搜索页面会利用用户提交的翻译。
- Pixiv 官方 2020 翻译社区说明（历史统计，不作为当前规模）：https://www.pixiv.co.jp/news/press-release/article/10214/ 。明确简体/繁体/韩语/英语标签译名与社区协作。
- Pixiv 官方翻译规范：https://www.pixiv.help/hc/ja/articles/360002987773-%E7%BF%BB%E8%A8%B3%E3%81%AE%E6%9B%B8%E5%BC%8F%E3%81%AE%E3%82%AC%E3%82%A4%E3%83%89%E3%83%A9%E3%82%A4%E3%83%B3%E3%81%AB%E3%81%A4%E3%81%84%E3%81%A6%E7%9F%A5%E3%82%8A%E3%81%9F%E3%81%84 。包含中简/繁规则；合法的中文译名可能混有英语官方名称。
- Pixiv 官方翻译使用说明：https://www.pixiv.help/hc/en-us/articles/360003046473-I-want-to-know-how-my-translation-will-be-used 。说明审核后的英译会显示，不能扩大推断为全部语言每项都为人工/自动翻译。
- Pixivpy 维护者源码（非官方 SDK / 非公开协议）：https://github.com/upbit/pixivpy/blob/master/pixivpy3/api.py 。set_accept_language 明确用于 tags.translated_name，语言示例 en-us、zh-cn；只摘相关方法，不保存或复用 SDK 中客户端凭据。
- PixivFE 维护者类型（非官方协议证据）：https://pkg.go.dev/codeberg.org/pixivfe/pixivfe/v3@v3.0.4/core#TagTranslations 。定义 en、ko、zh（简体）、zh_tw（繁体）、romaji；说明标签可能以 tags 项 translation 或 tagTranslation 词典两种形态出现。
- PixivNow 维护者接口材料（非官方逆向协议）：https://github.com/FreeNowOrg/PixivNow/blob/master/docs/pixiv-web-api.md 。GET /ajax/illust/{id} 的 tags.tags[].translation；search 响应的 tagTranslation；文档仅是协议示例，不能当作本次实时返回。

## 尚未验证及约束

- 无登录公开请求 `/ajax/illust/59580629?lang=zh`、`/ajax/tag/info` 和作品页在 web 工具不可读取；PowerShell 标准 HTTPS 请求失败于 SSL 建连。本轮没有绕过 TLS、captcha、登录或账号授权。
- 未发现可证明通用标签自动机翻及明确 machine 来源标志的官方公开材料；Pixiv 约稿自动翻译帮助与作品标签不是同一功能，不能套用。
- translated_name 字段自身没有显式语言/人工/机翻标记。仅凭 zh-cn 请求头不应声称任意返回值已证明为简体中文，更不能按纯拉丁文本猜测一定是英文（品牌/作品正式名称可合法使用英语）。
- 最小实现可保留 tags:string[] 为筛选身份，另在 source_metadata 记录原名对应译名、实际字段来源、请求语言、获取时间；详情另输出 tag_details 数组。Web 显式 translation.zh / tagTranslation[tag].zh 是中文身份的更强证据。
- 旧作品补译通过获批的信息刷新或已有同标签主站译名复用；离线/404 保留已存译名并标示取回时间，缺失不得伪造。译名变化应计入 catalogue revision，避免浏览器缓存不更新。
- 若缺中文仍必须灰黄色机翻且不使用其他在线服务，需要本地翻译模型；应先确定用户是否接受本地机器翻译模型（不等于让当前代理手写译名）。

## 本地机翻资源事实（候选，未安装/未下载模型）

- Argos 是离线库，需语言模型包；官方说明语言对平均约 100MB，能用 CPU，也可经英语中转但质量会下降：https://argos-translate.readthedocs.io/en/stable/index.html 、https://argos-translate.readthedocs.io/en/latest/source/gui.html 、https://argos-translate.readthedocs.io/en/latest/source/settings.html 。当前包索引 https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json 列出 ja-en 与 en-zh；本轮未核验单包散列/真实尺寸/运行内存，因此不承诺具体包大小或最终 EXE 增幅。
- 应只调用离线库，不启用它的 LibreTranslate/OpenAI 网络接口。模型许可证与 Windows 打包兼容须在选型试验中核验；本次没有安装全球或项目依赖。

运行 check_tag_loss.py 只执行源码中的静态纯函数，将结果保存在同目录 result.json。使用项目既有 `.devtools/v012-env/Scripts/python.exe` 运行通过：合成输入的 translated_name 在 _parse_illust 返回值中丢失。该复现不代表真实 Pixiv 返回值。
