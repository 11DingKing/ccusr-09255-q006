# 短内容会话策略引擎

该服务为多租户内容平台提供版本化的观看策略和会话执行能力。策略可以限制年龄、每日用量、单次时长与休息时段；会话在启动时固定策略版本，心跳按顺序结算观看时长，并把每日用量、事件发件箱和操作记录保存在 SQLite 中。监护授权、策略灰度、限制申诉和合规暂停模块共享可审计的状态变更规则。

## 家庭共享额度

家庭账号在一个**共享池总额度**内为多名成员分配观看时间，同时每名成员仍受自己策略中的年龄门槛、单次时长与个人日额度约束。

- 每个家庭有按家庭时区切分的每日共享池（`daily_pool_seconds`），跨本地午夜自动重置。
- 每名成员配置**个人保底** `floor_seconds`：扣减他人额度时会预留其尚未用满的保底，保底未用满时任何人不能侵占。
- 心跳结算在**同一个数据库事务**内同时写入个人日账本与家庭池账本；共享池通过条件 SQL 守卫（`ON CONFLICT … WHERE used + n <= cap`）配合 `BEGIN IMMEDIATE` 串行写事务，保证并发会话**绝不透支**。
- 移除成员只把关系置为 `REMOVED`，其当日已占用的共享池额度**不回退**；移除后该用户回到纯个人额度模式，并可重新加入家庭。
- 总额度调整支持乐观版本号（`expected_version`），且调整后的总额不得小于所有活跃成员保底之和。

### 家庭 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/families` | 创建家庭（名称、时区、共享池总额） |
| GET | `/v1/families/{id}` | 查询家庭配置与版本 |
| POST | `/v1/families/{id}/adjust` | 调整共享池总额（带 `expected_version`） |
| GET | `/v1/families/{id}/usage` | 查询共享池当日已用/剩余及各成员用量 |
| POST | `/v1/families/{id}/members` | 添加成员（`user_id`、`floor_seconds`） |
| GET | `/v1/families/members/{member_id}` | 查询成员关系 |
| POST | `/v1/families/members/{member_id}/floor` | 调整成员个人保底 |
| DELETE | `/v1/families/members/{member_id}` | 移除成员（已用量不回退） |
| GET | `/v1/families/users/{user_id}/usage` | 按用户查询其家庭池与个人用量 |

所有家庭接口与其它接口一样需要 `X-Tenant-ID` 头。心跳响应的 `extra` 中新增 `per_family_day`（家庭 ID → 本地日 → 秒），会话用量接口额外返回 `family_*` 字段。

## 运行方式

安装项目依赖后执行 `uvicorn spe.app:app --host 127.0.0.1 --port 8000`。默认数据库为当前目录的 `spe.db`，不需要额外服务。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q spe tests
```

自动化测试覆盖策略校验、版本固定、会话幂等、并发启动、乱序心跳、跨午夜结算、额度截断和进程重启后的状态恢复；家庭模块额外覆盖并发扣减不透支、保底预留、跨家庭时区日重置、成员移除不回退、关系/保底变更与重启恢复。
