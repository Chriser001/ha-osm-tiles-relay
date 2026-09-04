---
name: ha-map-tiles-cn-relay
title: HA 2026.9+ 地图瓦片中继（CN 网络）
description: Use when HA 2026.9+ map tiles return 502/503 or the built-in map is blank. Covers diagnosis (SNI RST, DNS layers), the CF Worker + monkeypatch fix, WAF interplay, and pitfalls.
category: devops
tags:
  - home-assistant
  - map-tiles
  - cloudflare-worker
  - china-network
  - openstreetmap
---

# HA map_tiles 中继修复（CN 网络）

## 症状

- HA 2026.9+ 内置地图空白，DevTools 里 `/api/map_tiles/tilejson.json`、
  `/api/map_tiles/vector/{z}/{x}/{y}.mvt`、`sprites/...` 返回 **502/503**
  （外部经 CF 等反代访问可能看到 503，直连 HA 是 502）
- 2026.9 之前地图还能用（旧版是浏览器直连 CARTO；2026.8.3 起 CARTO 要 API key 是另一回事）

## 根因链（2026.9 架构变更）

1. HA 2026.9 新增 `map_tiles` 集成（core PR #180441），地图底图从浏览器直连改为
   **HA core 服务端代理**：`/api/map_tiles/*` → OSMF `vector.openstreetmap.org`
   （TileJSON/矢量/glyphs/sprites，shortbread_v1）与 `tile.openstreetmap.org`（栅格）
2. 上游**硬编码**，`CONFIG_SCHEMA = empty_config_schema`，无任何配置项；
   HA 的 aiohttp session 无 `trust_env`，不读代理环境变量
3. 上游失败 → `views.py` 固定回 `HTTPStatus.BAD_GATEWAY`（502），失败只打 **debug** 级日志
4. CN 网络：GFW 对该两个域名的 SNI **无条件 RST**（不限目标 IP）→ 上游必失败

**2026.10 dev 仍无配置口子**（master 上 const.py/__init__.py 未变）。国内用户全挂，非个例。

## 诊断步骤（快速定位到"是 SNI 封锁"）

```bash
# 1. 端点状态（Bearer 用 HA 长效 token；token 查询参数 30 分钟轮换，重启即作废）
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  http://<ha>:8123/api/map_tiles/tilejson.json        # 502 = 上游失败

# 2. 上游连通性：TCP vs TLS 分层测（从盒子/Mac，先 unset 代理）
nc -z -G 4 <ip> 443                                    # TCP OPEN ≠ 可达
echo | openssl s_client -connect <ip>:443 -servername vector.openstreetmap.org 2>&1 | head -5
#   RST(write:errno=54) = SNI 封锁；换中性 SNI(example.org) 同 IP 若握手正常 → 确认 SNI 级
#   再发往任意非封锁 IP（同 SNI）也 RST → GFW 被动全域 SNI 过滤，DNS 换 IP 无效

# 3. DNS 分层（容器 → hassio_dns 172.30.32.3 → AdGuard 172.30.32.1）
docker exec homeassistant python3 -c "import socket;print(socket.gethostbyname_ex('vector.openstreetmap.org'))"
#   容器 gaierror(-5 'no usable address') 而 AdGuard 直查正常 = hassio_dns 负缓存

# 4. 组件日志看不到失败（debug 级）：临时开 logger debug 或直接改上游测试
```

封锁类型结论速查：

| 测试 | 结果 | 结论 |
|---|---|---|
| TCP 到 IP:443 | OPEN | 非 IP 封锁 |
| 中性 SNI 同 IP | 握手成功 | 非整 IP TLS 封锁 |
| OSM SNI 任意 IP | RST | **SNI 级封锁，DNS 固定无效，必须隧道/代理** |

## 修复架构（仓库 ha-osm-tiles-relay）

零 HA core 改动。三层：

1. **Cloudflare Worker**（`worker/index.js`）：按路径 1:1 转发到两个 OSMF 源，
   路径白名单：`/shortbread_v1/*`、`/styles/shortbread/*` → vector；
   `/^\d+/\d+/\d+\.png$/` → tile。替换 UA 为合规标识（OSMF tile policy）。
   挂在 **Custom domain**（如 `osm-tiles.<zone>`，自动建 DNS）。
2. **HA 自定义组件** `custom_components/osm_tiles_proxy`：
   `dependencies: ["map_tiles"]` 保证 core 先加载 views；`async_setup` 里改写
   `map_tiles.views` 模块的取值点（模块全局 `VECTOR_URL`/`TILEJSON_URL` 请求时读取；
   `MapTilesVectorView.upstream`/`RasterView.upstream` 是类定义时固化的 f-string，
   必须改**类属性**；`UPSTREAM_HEADERS` UA 追加白名单 token）。
   失败即打 ERROR 并保持原状（**fail-open**，升级不炸）。
3. **WAF 交互**：zone 级 UA 白名单会 403 HA core 的 UA。两种解法：
   WAF Allow 规则（Hostname equals relay）放行，**或**组件给 UA 追加白名单 token
   （默认 ` Firefox/1`；Worker 转发 OSMF 时换回自己的 UA）。WAF 在边缘缓存**之前**
   执行 → 保持 WAF 挡非白名单 UA，缓存 HIT 也 403，防刷有效。

## 缓存层级（验证过的行为）

| 层 | 有效期 | 失效条件 |
|---|---|---|
| 浏览器 | `private, max-age=604800`（7 天） | token 30 分钟轮换 → URL 变 → 全 miss |
| HA 内存 MapTilesCache | 32MB LRU，永不因过期丢弃 | HA 重启清空 |
| CF 边缘 | 透传 OSMF `public, max-age=1209600`（14 天）+ stale-while-revalidate | 换 colo 首次 MISS |
| OSMF 回源 | — | 三层全 miss 才发生，量极小 |

token 轮换机制（`TOKEN_CHANGE_INTERVAL=30min`，deque maxlen=2，重启重置为单个新 token）
是"为什么要有服务端缓存"的原因——浏览器缓存被 URL 轮换周期性作废。

## 部署/验证/回滚

见仓库 README。要点：

- 验证：5 条 `custom_components.osm_tiles_proxy` WARNING 日志 = patch 生效
- 端到端：`tilejson.json` 200（注意响应是 **gzip** 编码，curl 加 `--compressed`），
  `vector/12/<x>/<y>.mvt` 200（x/y 按瓦片坐标换算目标区域）
- 回滚：删组件目录 + 移除 configuration.yaml 两行 + `ha core restart`

## 坑清单（实测）

1. **hassio_dns（CoreDNS）负缓存**：新域名上线前若 HA/容器预读过（NXDOMAIN/空应答），
   CoreDNS 会缓存空应答 —— 重启 core **没用**，必须 `docker restart hassio_dns`。
   AdGuard 直查正常 ≠ 容器能解析。
2. **HA 重启后 token 作废**：查询参数 token 只在内存（重启重置），之前抓的 URL 全失效；
   验证用 `Authorization: Bearer` + 长效 token（`.storage/auth` 只存哈希，明文只在创建时
   给一次 —— 长效 token 存 ~/.hermes/.env 的 `HASS_TOKEN`）。
3. **tilejson 响应是 gzip**：HA 组件本地重建后固定 gzip（`_rebuild`），curl 不带
   `--compressed` 会拿到 gzip 字节（json 解析报 "Expecting value char 0"）。
4. **macOS curl DNS 抽风**：PAC 代理环境下 curl 偶发 `Could not resolve host` 而 dig 正常
   —— 从盒子测或换 dig/浏览器。
5. **CF Anycast colo 抖动**（移动线路常见 LHR/SEA 交替）：每换新 colo 首次 MISS。
6. **上游失败零日志**：views 只在 debug 打 `Upstream ... failed`；排障先确认是不是
   502 + 上游可达性，别在 HA 日志里找错误。
7. 矢量瓦片 maxzoom=14（`VECTOR_MAX_ZOOM`），z>14 请求直接 404，不是故障。

## 参考

- 完整实现：`github.com/Chriser001/ha-osm-tiles-relay`（custom_components/ + worker/）
- HA 源码：`homeassistant/components/map_tiles/{__init__,views,cache,const}.py`（2026.9.0）
- 上游讨论：home-assistant/core #180277、frontend #53800（CARTO API key → 换 OSMF 的来龙去脉）
