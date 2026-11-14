# iptv-sub · 自动维护的 IPTV 订阅

一个不依赖任何第三方包的小流水线：**GitHub Actions 定时抓取上游公开源 → 逐个实测 → 剔除失效/黑屏/无声 → 生成 m3u → 自动提交**。
车机上的 TiviMate / DIYP / APTV 填一个固定地址即可，之后它每次联网自动拉最新列表，你不用管。

---

## 它到底做了什么

1. **抓源**：拉取 `config.json` 里配置的 8 个上游清单（都是别人用 Actions 自动维护的）。
2. **归名**：把 `CCTV1`、`CCTV-1 综合`、`CCTV-1高清` 统一成 `CCTV-1`；只保留央视、卫视、以及白名单里的少量频道（县级台全丢）。
3. **实测**（每个源都真下载一段）：
   - 首包时延 —— 决定换台快不快；
   - 吞吐量 —— 决定会不会卡；
   - 解析 TS 的 PAT/PMT —— 判断**有没有画面**、**音轨是 AAC 还是 MP2/AC3**。
4. **过滤**：
   - 打不开 / 超时 / 分片太小（黑屏、静态图、错误页）→ 丢弃；
   - 音轨是 MP2 / AC3 / 私有流（安卓默认解不出声）→ 丢弃；
   - 一个源都不剩的频道 → 整个频道剔除。
5. **排序**：AAC 优先 → 首包快优先 → 码率高优先；每个频道只留最快的 3 路。
6. **输出**：`output/iptv.m3u`（TiviMate/DIYP/APTV 通用）、`output/iptv.txt`、`output/app_channels.json`（给自制 App 用）、`output/report.json`（详细测速报告）。

---

## 目录

```
├─ .github/workflows/update.yml   # 定时任务
├─ config.json                    # 上游清单 / 保留源数 / 频道白名单
├─ scripts/fetch.py               # 全部逻辑（纯标准库）
└─ output/                        # 生成结果（由 Actions 自动提交）
   ├─ iptv.m3u
   ├─ iptv.txt
   ├─ app_channels.json
   └─ report.json
```

---

## 订阅地址（仓库建好后）

把 `你的用户名` / `iptv-sub` 换掉即可，四条任选一条，都是固定不变的 https：

| 用途 | 地址 |
|---|---|
| jsDelivr CDN（推荐，国内一般能直接访问） | `https://cdn.jsdelivr.net/gh/你的用户名/iptv-sub@main/output/iptv.m3u` |
| 国内镜像加速 | `https://gh-proxy.com/raw.githubusercontent.com/你的用户名/iptv-sub/main/output/iptv.m3u` |
| GitHub 原始地址（国内常被墙） | `https://raw.githubusercontent.com/你的用户名/iptv-sub/main/output/iptv.m3u` |
| 配了 Cloudflare Pages 后 | `https://iptv-sub.pages.dev/iptv.m3u` |

> 注意：jsDelivr 对 `@main` 有缓存，通常几分钟内刷新；要立刻看到最新结果用 gh-proxy 那条。

---

## 车机软件怎么填

- **TiviMate**：设置 → 播放列表 → 添加播放列表 → M3U 播放列表 → 粘贴地址 → 打开「自动更新播放列表」，间隔 6 小时。
- **DIYP / 影视TV**：设置 → 直播源接口 → 粘贴地址 → 保存，重启自动拉取。
- **APTV**（iOS/车机）：订阅 → 添加订阅 → 填地址。

---

## 想改什么，只改 `config.json`

```jsonc
{
  "sources": [...],                  // 上游 m3u 清单，可增删
  "max_candidates_per_channel": 8,   // 每个频道最多测几个源（越大越全，越慢）
  "keep_best_per_channel": 3,        // 每个频道最终保留几个
  "probe_timeout": 8,                // 单源超时（秒）
  "probe_bytes": 2500000,            // 测速时下载多少字节
  "threads": 48,                     // 并发数
  "other_policy": "whitelist",        // 非央视/卫视怎么处理：whitelist 只留白名单 / all 全留
  "other_whitelist": ["广东珠江", ...] // other_policy=whitelist 时生效
}
```

改完直接 commit 推到 `main`，会立刻触发一次（workflow 的 `push` 事件），也可以到 Actions 页面点 **Run workflow**。
