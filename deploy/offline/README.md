# AI Resume ParserTag 离线部署包

本目录只使用已导出的 `linux/amd64` 镜像，不会从外网拉取镜像。Mac ARM Docker Desktop 通过 amd64 仿真运行，内网 Linux AMD64 服务器原生运行。

## 使用方式

1. 编辑 `.env`，填写内网模型服务地址和 API Key。
2. 执行 `./offline.sh deploy`，自动导入镜像、后台启动并等待健康检查。
3. 查看状态：`./offline.sh status`。
4. 查看日志：`./offline.sh logs`。
5. 停止服务：`./offline.sh stop`；删除容器但保留数据：`./offline.sh down`。

默认健康地址：`http://localhost:21804/health`。

重复执行仓库根目录的 `scripts/package-offline.sh` 时，只替换镜像、部署模板和非
运行时 Schema；已有 `.env`、日志、上传、结果及 `tag_config_runtime.json` 保留。
如需重置标签运行时配置，应通过配置 API 或在备份后显式删除该文件。

## 持久化目录

- `data/logs` → `/app/logs`
- `data/uploads` → `/app/uploads`
- `data/results` → `/app/results`
- `data/schemas` → `/app/schemas`

Mac 与 Linux 使用完全相同的相对目录挂载逻辑。迁移时整体复制本目录即可保留配置和数据。

## 内网传输

将打包脚本生成的 `ai-resume-parsertag-offline-amd64.tar.gz` 上传到 Linux AMD64 服务器并解压：

```sh
mkdir ai-resume-parsertag-offline
tar -xzf ai-resume-parsertag-offline-amd64.tar.gz -C ai-resume-parsertag-offline
cd ai-resume-parsertag-offline
./offline.sh deploy
```
