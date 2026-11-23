# 胶片档案交档

馆内用一套**档案卷**保存多部成片共用的胶片素材。交档时填写成片号并上传剪辑
CSV，系统为每个原卷建立一卷从 0 开始编号的档案卷，把新帧连续追加到卷尾，并
给出按单号可查的取帧单。

## 规则实现对照

| 需求 | 实现 |
| --- | --- |
| 填成片号、上传剪辑 CSV，每行镜头号/原卷号/起帧/帧数 | 交档页表单，CSV 全量校验后才落库 |
| 每个原卷对应一个档案卷，档案帧号从 0 起 | `reels.next_frame` 初始为 0，`archive_segments` 从 0 起铺满 |
| 已收帧沿用档案帧号，新帧连续追加到卷尾 | 覆盖索引命中的区间原地复用；未覆盖空隙只在卷尾分配 |
| 同一帧一单多次引用 / 两单并发引用只拷一次 | 覆盖索引 + `BEGIN IMMEDIATE` 单事务；并发测试覆盖 |
| 按单号查本次拷贝的原卷帧段与落点 | `/orders/<单号>` 与 `/api/orders/<单号>` 的 `copies` |
| 查每个镜头依次从档案卷取哪些帧段以拼回原帧序 | 同页 `shots[].segments`，按 `seg_index` 顺序拼接 |
| 清单与索引一同生效，失败时旧单和卷尾不变 | 单事务写入 `orders/shots/order_copies/shot_segments/archive_segments/reels`，异常整体回滚 |
| 全程按帧段核算，不逐帧展开 | 所有计算基于半开区间 `[start, start+count)`，与帧数量级无关 |

## 核心概念

* **原卷帧段**：CSV 中的 `[起帧, 起帧+帧数)`，半开区间。
* **档案卷尾**：`reels.next_frame`，该卷第一个尚未使用的档案帧号。
* **覆盖索引**：`archive_segments` 记录“原卷帧段 → 档案帧段”的不重叠映射。
  命中即可复用；未命中才在卷尾分配并写入。

取帧单里的每一段都是连续档案区间，按 `seg_index` 依次读取拼接即可还原该镜头
的原始帧序，无需逐帧处理。

## 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

python3 -m archive          # 开发服务器，默认 0.0.0.0:8000
# 打开 http://localhost:8000
```

环境变量：

* `ARCHIVE_DB`：SQLite 路径，默认 `data/archive.sqlite3`
* `SECRET_KEY`：会话密钥；未设置时会在数据库旁生成并持久化
  （`*.sqlite3.secret`），确保多 worker 共享同一密钥
* `PORT` / `HOST`：开发服务器监听地址

## Docker Compose

```bash
docker compose up --build
# http://localhost:8000
```

数据库文件位于命名卷 `archive-data`，重建容器不会丢失。生产部署请通过
`SECRET_KEY` 环境变量显式提供密钥（`docker compose` 默认值仅供本机试用）。

## 测试

```bash
python3 -m pytest -q
```

覆盖：首单从 0 分配、复用+追加、单内重复引用去重、多原卷、取帧单顺序、校验
失败回滚、事务中途异常回滚、重复单号、超大帧段（5000 万帧）不逐帧展开、随机
不变量检查，以及多线程 / 多 gunicorn worker 并发交档。

## CSV 格式

```csv
镜头号,原卷号,起帧,帧数
S01,R001,0,120
S02,R001,100,50
```

* 固定四列，支持常见中英文表头别名；UTF-8（含 BOM）或 GB18030 编码。
* 空行忽略；支持带引号字段与 CRLF。
* 起帧、帧数为非负整数，帧数必须大于 0。
* 任一行出错则整单拒绝，页面会一次列出所有问题，档案卷不发生改动。

样例见 `samples/edit.csv`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 交档页、按单号查询、最近交档 |
| POST | `/submit` | 交档（multipart：`order_no`、`film_no`、`csv_file`、`csrf_token`） |
| GET | `/orders/<单号>` | 单号详情页 |
| GET | `/reels/<原卷号>` | 档案卷覆盖索引与卷尾 |
| GET | `/api/orders/<单号>` | 单号 JSON |
| GET | `/api/reels/<原卷号>` | 档案卷 JSON |

## 表结构

* `orders`：单号、成片号、时间、行数（单号唯一）。
* `shots`：本单每个镜头行的原始帧段。
* `order_copies`：本单需要拷贝/沿用的原卷帧段与档案落点，`is_new` 区分本次
  新建还是复用（按原卷帧段合并后每个区间一行）。
* `shot_segments`：每个镜头按顺序应读取的档案帧段。
* `reels`：每原卷一卷，`next_frame` 为卷尾。
* `archive_segments`：全局覆盖索引，含触发器保证同卷内原卷帧段、档案帧段均
  不重叠且不越过卷尾。
