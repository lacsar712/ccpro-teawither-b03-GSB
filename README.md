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

**业务规则（表单与模型同源）**：

- **实测含水率取值范围**：`actualMoisture` 留空允许保存；一旦填写必须**大于 0 且不超过 100**。该规则只有一个源头——模型字段上的 `validate_actual_moisture`：
  - 表单提交路径：`ModelForm` 自动挂载同一 validator；
  - 直接模型保存路径：`WitherBatch.save()` 调 `full_clean()` 触发同一 validator。
  - 两条路径对非法值都以同一句中文拒绝：`实测含水率若填写须大于 0 且不超过 100。`
- **可下槽资格**：槽位置为 `ready`（可下槽）时，最新批次（按 `startedAt, id` 倒序）实测含水率须**已填写且不超过 40%**，否则 `Trough.clean` 抛出中文 `ValidationError`。空实测可以保存批次，但不会让槽变成可下槽。
- **写完立即影响资格**：批次 `save()`/`delete()` 后立即重算所属槽资格——若原可下槽槽的最新实测变为空或高于 40%，状态自动回退为「萎凋中」。
- **读数同源**：槽改态校验与页面（槽列表「最新批次实测」列、批次详情/列表）都经由 `Trough.latest_batch()` / `latest_actual_moisture()` 取数，不存在改态读数与页面显示分叉。
- **首页对账**：首页「可下槽」只统计 `status=ready` 的槽位；点击该卡片跳转槽列表 `?status=ready`，筛选行数与首页计数同一查询口径，可逐行对账。

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有茶园则只保证账号存在。亦可在环境变量 `TEAWITHER_AUTO_SEED=1` 时于 `post_migrate` 自动播种。

种子内含两笔**资格反例**：

- `A-02`：最新批次实测为空（缺实测槽）——批次可保存，但槽不可下槽；
- `B-01`：最新批次实测 `42.00%`（越界实测，越过 40% 门槛；42 仍在 0–100 字段合法范围内故批次可存）——槽不可下槽。

另含正向对照：`A-01`（37.5%，保留萎凋中）与 `B-02`（34.8%，置为可下槽）。

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
