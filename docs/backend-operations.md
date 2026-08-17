# 后端与 MySQL 开发运维记录

更新日期：2026-08-05

```
MCP.py
      ↓
backend/mcp_suning/config.py 读取 backend/.env
      ↓
database.py 创建 SQLAlchemy Engine
      ↓
PyMySQL 连接 127.0.0.1:3306
      ↓
Docker 把端口转发到 MySQL 容器
      ↓
MySQL 读取/查询数据库
      ↓
数据保存在 Docker Volume
```
docker部分负责运行mysql数据库服务器(配置compose.yaml)
```yaml
image: mysql:8.0.45
container_name: suning-mysql
# 以后可以通过这个名字操作它：
# docker logs suning-mysql
# docker exec -it suning-mysql mysql -uroot -p

#MySQL 数据实际保存在 Docker Volume
volumes:
  - suning_mysql_data:/var/lib/mysql

```
127.0.0.1:3306     →     容器内部 3306
WSL/Python 访问           MySQL 实际监听

三个数据库账号
| 用户名 | 用途 | 权限 |
|---|---|---|
| `root` | 数据库最高管理 | 全部权限 |
| `suning_schema_admin` | 建表、导入模拟数据 | 当前业务库的管理权限 |
| `suning_readonly` | MCP 日常运行 | `SELECT`、`SHOW VIEW` |

## MySQL 日常操作命令

### 1. 进入 MySQL 配置目录

```bash
cd /home/ctrau/suning-hermes-agent/backend/infra/mysql
```

### 2. 查看 MySQL 容器状态

```bash
docker compose ps
```

正常情况下，MySQL 应显示为 `healthy`。

### 3. 启动 MySQL

```bash
docker compose start
```

如果容器还没有创建，执行：

```bash
docker compose --env-file .env up -d
```

### 4. 停止 MySQL

停止容器但保留数据库数据：

```bash
docker compose stop
```

### 5. 查看 MySQL 日志

```bash
docker compose logs mysql
```

持续查看实时日志：

```bash
docker compose logs -f mysql
```

按 `Ctrl+C` 退出日志查看。

### 6. 进入 MySQL root 管理控制台

```bash
docker exec -it suning-mysql mysql -uroot -p
```

按照提示输入 root 密码。

### 7. 使用只读账号连接业务数据库

```bash
mysql \
  -h 127.0.0.1 \
  -P 3306 \
  -u suning_readonly \
  -p \
  suning_aftersale
```

按照提示输入 `suning_readonly` 的密码。

### 8. 启动订单 MCP

先进入后端目录：

```bash
cd /home/ctrau/suning-hermes-agent/backend
```

然后按模块启动：

```bash
uv run python -m mcp_suning.servers.order
```

### 8.1 使用 systemd 统一管理全部 MCP

仓库提供 `backend/infra/systemd/suning-mcp@.service` 模板和 `suning-mcp.target`。安装后，一个
target 会管理 `order`、`aftersale`、`product`、`logistics`、`payment`、`timeline` 六个实例：

```bash
cd /home/ctrau/suning-hermes-agent/backend
sudo install -m 0644 infra/systemd/suning-mcp@.service /etc/systemd/system/
sudo install -m 0644 infra/systemd/suning-mcp.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suning-mcp.target
```

常用操作：

```bash
systemctl status 'suning-mcp@*.service'
sudo systemctl restart suning-mcp.target
sudo systemctl stop suning-mcp.target
journalctl -u suning-mcp@timeline.service -f
```

模板使用 `/home/ctrau/suning-hermes-agent/backend/.venv`、`backend/.env` 和用户 `ctrau`。若部署
位置或运行用户不同，安装前修改模板中的路径、`User` 与 `Group`。

### 9. 检查 Docker 是否正常

```bash
docker version
```

正常情况下应同时显示 Docker Client 和 Server 信息。

### 10. 删除容器但保留数据库数据

```bash
cd /home/ctrau/suning-hermes-agent/backend/infra/mysql
docker compose down
```

> 注意：不要随意执行 `docker compose down -v`。  
> `-v` 会删除 MySQL 数据卷，数据库表和数据都会丢失。
