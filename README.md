# TeaWither-01 · 茶萎凋台账

Django 5 + PostgreSQL 服务端渲染应用：Templates + HTMX + 自定义 CSS，无 Vue/React SPA。

## 技术栈

- Django 5、PostgreSQL
- Session 登录
- HTMX（CDN）局部刷新列表
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4100** |
| Postgres | **5440**（容器内 5432） |

数据库账号：`teawither` / `teawither` / 库名 `teawither`

## 快速启动

```bash
cd TeaWither/TeaWither-01
docker compose up --build -d
```

浏览器打开：http://localhost:4100

演示账号：

- `admin` / `123456`（超级用户）
- `witherer` / `123456`（普通用户）

容器启动时会自动：`migrate` → `seed_data` → `collectstatic` → `gunicorn`

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 确保本机 Postgres 监听 5440，或先 docker compose up -d db
set POSTGRES_HOST=localhost
set POSTGRES_PORT=5440
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:4100
```

## 业务模型

1. **Garden（茶园）**：`name`、`altitudeBand`、`notes`
2. **Trough（萎凋槽）**：归属茶园、`troughCode`、`cultivar`、`loadKg`、状态 `loading|withering|ready`；同一茶园内槽位编号唯一
3. **WitherBatch（萎凋批次）**：归属槽位、`startedAt`、`targetMoisture`、`actualMoisture`（可空）、`rollGrade`

### 实测含水率校验（表单 / 模型同一套规则）

- `actualMoisture` **可留空**保存；一旦填写，必须 **大于 0 且不超过 100**。
- 规则只有一份：`apps/gardens/models.py` 中的 `validate_actual_moisture` 校验器，
  同时挂在模型字段上（`ModelForm` 自动继承）与 `full_clean()` 链路上。
- 因此两条路径用同一套中文文案拒绝：
  1. **表单提交**（批次新建/编辑页 POST）：字段下显示「实测含水率填写时必须大于 0 且不超过 100。」
  2. **直接模型保存**（`WitherBatch(...).save()` / `objects.create()` / shell / admin）：
     `save()` 内调用 `full_clean()`，抛同一文案的 `ValidationError`，数据不会落库。

### 可下槽资格（单一数据源）

- 判定入口唯一：`Trough.latest_batch()` → `Trough.latest_actual_moisture()`
  → `Trough.ready_block_reason()` / `is_ready_eligible()`。
- 槽改态入口（槽编辑页）、槽详情页、批次详情页、槽列表、模型 `clean()`
  全部读取这组方法，改态读到的最新实测与页面显示不会分叉。
- 规则：最新批次（按 `startedAt` 倒序、`id` 决胜）实测为空或没有批次 →
  不具备资格；实测高于 40% → 不具备资格；已填写且 ≤ 40% → 具备资格。
- **写完立即影响资格**：批次新建/编辑/删除后立即重算所属槽——
  已是「可下槽」但最新实测被清空/改高时，状态自动退回「萎凋中」；
  新具备资格的槽只提示，不替用户改态（改态仍须在槽编辑页显式操作并过同一校验）。

### 首页计数与列表对账

- 首页「可下槽」只统计 `status=ready` 的槽（`Trough.objects.ready()`），
  与萎凋槽列表 `?status=ready` 是同一个 QuerySet 方法，行数必然一致；
  首页数字本身即链接到该筛选视图。

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有茶园则只保证账号存在。亦可在环境变量 `TEAWITHER_AUTO_SEED=1` 时于 `post_migrate` 自动播种。

种子内含两个关键样例：

- **缺实测的槽** A-02：最新批次 `actualMoisture=None`（空值允许保存），
  但该槽不具备可下槽资格，强行改态会被中文拒绝。
- **越界实测尝试**：播种时尝试以 `actualMoisture=120` **直接走模型保存**，
  被同一中文文案拒绝并在命令行打印拒绝信息，批次不落库（演示双路径同口径）。
- B-02 为正常可下槽槽位（实测 34.80%）。

## 目录结构

```
TeaWither-01/
  manage.py
  requirements.txt
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  config/           # 项目配置
  apps/gardens/     # 模型、视图、种子命令
  templates/        # Django 模板
  static/css/       # 自定义样式（茶绿色顶栏）
```
