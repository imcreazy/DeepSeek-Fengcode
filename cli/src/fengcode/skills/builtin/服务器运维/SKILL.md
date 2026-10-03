---
name: 服务器运维
description: 在远程 Linux 主机上排查故障、看日志、查资源、安全改配置的标准流程。
when_to_use: 需要连接服务器、排查线上问题、看服务状态、部署或改配置时
version: 1.0.0
tags: [运维, 服务器, SSH, 排查, 日志]
---

# 服务器运维

## 铁律
1. **先读后写**。任何修改前先看当前状态：配置文件原文、服务状态、日志尾部。
2. **改配置先备份**：`cp x.conf x.conf.bak-$(date +%Y%m%d-%H%M%S)`。
3. **改动可回滚**：知道怎么退回去再动手；不确定就先在测试路径试。
4. **禁止的操作**：`rm -rf` 打头清理、直接编辑正在运行的关键配置不 reload、
   清空日志/数据库、改防火墙把自己锁在外面。这些会触发审批门或直接拒绝。

## 标准排查顺序
```
1. ssh(action="check", host="...")           # 确认能连上
2. ssh(action="info", host="...")            # 看负载/内存/磁盘/进程
3. ssh(action="exec", command="systemctl status 服务名 --no-pager")
4. ssh(action="tail", remote_path="/var/log/xxx.log", lines=200)
5. 定位后：改配置 → reload（不是 restart）→ 立刻验证
```

## 常用命令速查
```bash
# 资源
uptime; free -h; df -h; du -sh /path/* | sort -rh | head
top -bn1 | head -20
# 端口与连接
ss -lntp                    # 监听端口（代替 netstat）
ss -s                       # 连接统计
# 进程
ps aux --sort=-%mem | head
systemctl status X --no-pager; journalctl -u X -n 200 --no-pager
# Docker
docker ps --format '{{.Names}}\t{{.Status}}'
docker logs --tail 200 容器名
docker stats --no-stream
# 日志
tail -f /var/log/x.log      # 注意：follow 模式会挂住，用 tail 看固定行数
grep -n "ERROR" /var/log/x.log | tail -50
```

## 修改配置的正确姿势
1. 备份 → 2. 用 `sed -i` 或 sftp 写入（**避免用 `>` 直接覆盖**）→
3. 语法检查（nginx：`nginx -t`；systemd：`systemctl daemon-reload`）→
4. reload → 5. 验证（curl 一下 / 看日志有没有报错）→ 6. 不通过就还原备份。

## 安全
- 不要把服务器密码、密钥打印到输出里。
- 不要把公网服务的端口直接暴露调试。
- 涉及资金/用户数据的操作，先说明影响再执行。
