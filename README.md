# ha-osm-tiles-relay

**让 Home Assistant 2026.9+ 的内置地图在 OpenStreetMap 瓦片源被封锁/不可达的网络里正常工作。**

Home Assistant 2026.9 起，内置地图（Map dashboard / Map card）的底图由新的
[`map_tiles`](https://www.home-assistant.io/integrations/map_tiles) 集成**服务端代理**：
浏览器请求 `/api/map_tiles/*`，HA core 转发到 OpenStreetMap Foundation 的瓦片服务器
（`vector.openstreetmap.org` / `tile.openstreetmap.org`，均托管在 Fastly 上），无任何配置项。

在中国大陆等网络里，这两个域名的 TLS SNI 会被 GFW 无条件 RST（与目标 IP 无关），
HA 的代理拿不到上游，`/api/map_tiles/*` 全部返回 **502/503**，地图空白。

本仓库提供一条**不改 HA core 一行代码**的修复路径：

```
浏览器 ──▶ HA /api/map_tiles/*（token + 32MB 内存缓存，原版逻辑）
              │ 上游 URL 被组件改写（monkeypatch）
              ▼
      https://<relay-host>/...（你的 Cloudflare 域名）
              │ CF 边缘（WAF 前置 + 14 天边缘缓存）
              ▼
      Cloudflare Worker（osm-tiles-relay，1:1 转发）
              │ 从 CF 边缘出口（墙外）
              ▼
      vector.openstreetmap.org / tile.openstreetmap.org
```

## 目录结构

```
├── custom_components/
│   └── osm_tiles_proxy/        # HA 自定义组件：把 map_tiles 上游改写为 relay 域名
│       ├── __init__.py         #   启动时 monkeypatch views 模块的 URL 取值点
│       └── manifest.json       #   声明 dependencies: ["map_tiles"] 保证执行顺序
├── worker/
│   └── index.js                # Cloudflare Worker：按路径 1:1 转发到 OSMF
└── skills/
    └── ha-map-tiles-cn-relay/  # Hermes/agent 技能：完整排障手册（SKILL.md）
```

## 原理

HA 的 `map_tiles` 组件（`homeassistant/components/map_tiles/`，2026.9 新增，作者 bramkragten）
把上游写死为 OSMF 域名，且 `CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)` —— 没有任何配置口子；
HA 的 aiohttp session 也不读代理环境变量。上游失败时组件固定返回 `502 Bad Gateway`
（源码 `views.py`，且失败只打 debug 级日志，默认日志里看不到）。

本组件不碰 HTTP 层：它利用 HA 的依赖加载顺序（`dependencies: ["map_tiles"]`），在
`map_tiles.views` 模块已 import 后改写其中的取值点：

| 取值点 | 类型 | 被谁使用 |
|---|---|---|
| `views.VECTOR_URL` | 模块全局（请求时读取） | glyphs / sprite 的 URL 构造 |
| `views.TILEJSON_URL` | 模块全局 | TileJSON 视图 |
| `views.MapTilesVectorView.upstream` | 类属性（类定义时 f-string 固化） | 矢量瓦片 |
| `views.MapTilesRasterView.upstream` | 类属性 | 栅格瓦片 |
| `views.UPSTREAM_HEADERS["User-Agent"]` | 模块全局 dict | 追加 WAF 白名单 token（见下） |

Worker 收到 `https://<relay-host>/shortbread_v1/...`、`/styles/...` 时转发给
`vector.openstreetmap.org`，收到 `/{z}/{x}/{y}.png` 时转发给 `tile.openstreetmap.org`，
其余路径 404。它替换 UA 为带联系方式的合规标识（满足 OSMF tile policy）。

## 部署

### 1. Cloudflare Worker

1. Workers & Pages → Create Worker → 命名（如 `osm-tiles-relay`）
2. Edit code → 粘贴 `worker/index.js` → Deploy
3. Settings → Domains & Routes → Add → **Custom domain**，如 `osm-tiles.example.com`
   （会自动创建 DNS 记录，子域位于你的 CF zone 下即可）

### 2. HA 自定义组件

```bash
# 拷贝到 HA config 的 custom_components/ 下
scp -r custom_components/osm_tiles_proxy root@<ha>:/mnt/data/supervisor/homeassistant/custom_components/

# configuration.yaml 追加
cat >> configuration.yaml << 'EOF'
osm_tiles_proxy:
  url: https://osm-tiles.example.com
EOF

ha core restart
```

重启后日志里应出现 5 条 `WARNING (MainThread) [custom_components.osm_tiles_proxy]`，
列出被改写的 URL —— 即补丁已生效。

### 3. WAF / UA 白名单

若你的 CF zone 配了 UA 白名单类 WAF 规则（本组件内置的 UA
`HomeAssistant/2026.9.0 (+https://www.home-assistant.io; ...)` 不在白名单内），二选一：

- **推荐**：WAF 加一条 `Hostname equals <relay-host>` → Allow，拖到白名单规则之前；
- 或：保持 WAF 挡扫描（非白名单 UA 一律 403，缓存命中前执行），给组件 UA 追加白名单 token
  —— 本组件默认在 `views.UPSTREAM_HEADERS` 的 UA 后追加 ` Firefox/1`（模块注释里有说明）。
  Worker 转发给 OSMF 时会替换成自己的 UA，OSMF 侧看到的仍是合规标识。

### 4. 验证

```bash
# 浏览器先硬刷新一次地图仪表盘，或：
curl -H "Authorization: Bearer <long-lived-token>" \
  http://<ha>:8123/api/map_tiles/tilejson.json          # → 200 JSON，tiles 指向 /api/map_tiles/vector/...
curl -H "Authorization: Bearer <long-lived-token>" -o /dev/null -w '%{http_code}\n' \
  http://<ha>:8123/api/map_tiles/vector/3/1/3.mvt        # → 200
```

## 缓存与流量

| 层 | 有效期 | 说明 |
|---|---|---|
| 浏览器 | 7 天（`private, max-age`） | token 每 30 分钟轮换 → URL 变化 → 浏览器缓存周期性作废 |
| HA 内存 | 进程内 32MB LRU，重启即空 | stale-while-refresh，token 轮换的兜底 |
| CF 边缘 | 14 天（透传 OSMF 的 `public, max-age=1209600`） | 已实测生效；HA 重启后首拉也由边缘兜住 |
| OSMF | — | 实际回源极少 |

WAF 在边缘缓存**之前**执行 —— 打缓存 HIT 也会被非白名单 UA 挡住，可放心当公共反代用。

## 升级与回滚

- **HA 升级**：`custom_components/` 原样保留，每次启动自动重新打补丁。若 HA 重构了
  `map_tiles` 内部结构，组件会打 ERROR 日志并**保持原状**（fail-open，回到未打补丁的行为），
  不会启动失败。
- **回滚**：删除 `custom_components/osm_tiles_proxy/` + 移除 `configuration.yaml` 两行 +
  `ha core restart`；Worker 在 CF 控制台删除即可。

## 已知坑（详见 skills/ 手册）

1. **hassio_dns 负缓存**：域名创建前若 HA 预读过（DNS 空应答被 CoreDNS 缓存），只重启
   core 无效，需 `docker restart hassio_dns` 清缓存。
2. macOS 上 curl 偶发 `Could not resolve host` 而 `dig` 正常（PAC 代理环境），从 HA 盒子
   上测或换用 dig/浏览器。
3. CF Anycast 会让连续请求落在不同 colo（如 LHR/SEA），每换新节点第一次 MISS，之后 HIT。
