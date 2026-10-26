# zotero-auto-collector

[中文](#简介) | [English](#about)

定时采集学术文献增量（OpenAlex + arXiv），按关键词过滤、去重后，**自动写入你的 Zotero 云端库**——本地 Zotero 开启同步后新文献自动出现。支持多通道：不同关键词可分别写入个人库或不同的群组库（协作场景：直接投喂给实验室/合作者的共享库）。

纯 Python 标准库，零第三方依赖，单文件部署，适合放在任何常开的小型 VPS / 树莓派 / NAS 上。

## 简介

**它解决什么**：追新文献靠人肉每天刷 arXiv/Google Scholar 既费时又容易漏。本工具每天定时查询"昨天全世界新发表/新提交"的文献，命中你的关键词就自动进 Zotero 库，打上 `自动采集` + 日期 + 关键词标签。你每天只需花 5 分钟过一遍标签，值得读的拖进分类，不要的删掉——**机器管广度，人管取舍**。

```
[你的服务器: cron 每日]  OpenAlex + arXiv 查昨日增量
        → 关键词过滤 → 双层去重（本地 state.db + 云端标题查重）
        → 按通道写入 Zotero 云端（Web API v3，只写元数据不传PDF）
[你的电脑]  Zotero 客户端开启同步 → 新文献自动出现
```

### 特性

- **真增量**：每日只查昨日窗口，配合本地 state.db 与云端标题双层去重，重复运行不产生重复条目
- **多通道分流**：不同关键词组可写入个人库或任意群组库（`group:<GroupID>`），协作免"发送"
- **只写元数据**：标题/作者/摘要/DOI/OA链接，不吃 Zotero 免费 300MB 附件额度；OA PDF 链接写在条目 Extra 字段
- **零依赖**：Python 3.8+ 标准库，scp 上去就能跑
- **小磁盘友好**：日志超 1MB 自动截半；失败退避重试（429/503）

## About

A daily incremental academic paper collector that writes matched papers straight into your Zotero cloud library via the official Web API. Sources: OpenAlex + arXiv. Multi-channel routing lets different keyword sets feed your personal library or any group library (great for sharing with your lab). Zero dependencies beyond Python's standard library.

## 快速开始

### 1. 准备 Zotero 凭据（约 3 分钟）

1. 注册/登录 [zotero.org](https://www.zotero.org)，本地客户端登录并开启同步
2. 打开 [zotero.org/settings/keys](https://www.zotero.org/settings/keys)，页面顶部记下 **userID**（一串数字）
3. 点 **Create New Private Key**：勾选 *Allow library access* + *Allow write access*（要用群组库则在下方群组区授权对应组 Read/Write），保存并复制 Key

### 2. 配置

```bash
git clone https://github.com/FDogeLover/zotero-auto-collector.git
cd zotero-auto-collector
cp .env.example .env
vim .env   # 填 userID / API Key / 至少一个通道的关键词
```

配置示例（完整字段见 [.env.example](.env.example)）：

```ini
ZOTERO_USER_ID=1234567
ZOTERO_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxx
CHANNEL_1_NAME=main
CHANNEL_1_KEYWORDS=graph neural network, federated learning
CHANNEL_1_TARGET=user
```

### 3. 手动试跑

```bash
python3 collector.py
```

正常输出：`候选: N 条` → `完成：写入 N 条 | 去重跳过 0 条`。去你的 Zotero 里点同步看看。

### 4. 每日定时（Linux crontab）

```bash
crontab -e
# 每天 08:05 采集昨日增量
5 8 * * * /usr/bin/python3 /path/to/collector.py >> /path/to/cron.log 2>&1
```

systemd timer、Windows 任务计划同理——本质就是每天跑一次 `python3 collector.py`。

## 配置参考

| 变量 | 必填 | 说明 |
|------|------|------|
| `ZOTERO_USER_ID` | ✅ | 数字 ID，见 zotero.org/settings/keys 页面顶部 |
| `ZOTERO_API_KEY` | ✅ | 需 library read + write 权限 |
| `CHANNEL_<N>_NAME` | | 通道名（会作为标签打在条目上），默认 `channelN` |
| `CHANNEL_<N>_KEYWORDS` | ✅ | 逗号分隔；建议 2–5 个起步，多了噪音大 |
| `CHANNEL_<N>_TARGET` | | `user`（默认，个人库）或 `group:<GroupID>` |
| `OPENALEX_MAILTO` | | 你的邮箱，进 OpenAlex 礼貌池获得更宽限速 |

## 设计细节与已知取舍

- **关键词是多词短语时**，OpenAlex 按语义检索、arXiv 按整句精确匹配（自动加引号）——建议用领域常用名词短语
- **宕机漏天**：每日只查昨日窗口，若某天没跑成，那天就漏了（v1 取舍）。补漏可手动把运行日的前几天日期改进去跑
- **预印本与正式版**：按 arXiv ID / OpenAlex ID 去重，版本更新不会重复入库，元数据也不会自动更新
- **ROS/深度学习这类超高频词慎用**：日命中上千条会淹库；先用窄词起步再放宽
- **只写元数据**是刻意设计：Zotero 免费 300MB 只限附件，元数据不限量；OA PDF 链接在条目 Extra 字段，想读时用 Zotero 的"查找可用 PDF"现抓

## 使用礼仪

- OpenAlex 请求带 `mailto` 参数（进礼貌池，10 req/s）；本工具每个关键词间隔 1 秒
- Zotero API 单批写入 ≤50 条、批间 sleep 2 秒——远低于官方限额
- 请勿把 `MAILTO` 换成别人的邮箱

## License

[MIT](LICENSE)
