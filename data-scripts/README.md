# data-scripts

针对本地历史数据快照（cc-switch 代理日志、模型目录）的一次性 Node.js 分析脚本。**数据源是一次性快照，脚本以保留为主，不做工程化。**

## 可复用脚本

- `agg.js` —— 聚合 cc-switch 代理请求日志，按模型统计 请求数/成功率/平均延迟/最近调用，并与模型目录按别名合并。

  路径默认是作者机器的历史绝对路径，可用环境变量覆盖：

  ```bash
  CC_SWITCH_DB=/path/to/cc-switch.db MODELS_JSON=/path/to/models.json node agg.js
  ```

  依赖：`sqlite3` CLI 在 PATH 上。

## 历史探索残留

`gs*.js`、`inspect.js`、`dump.js`、`full.js`、`analyze.js` 是一次性探索脚本（硬编码 .codex/.cc-switch 状态文件路径），仅供考古，不再维护。

## 数据文件

- `models.json` / `aqua_models.json` —— 分析当时的数据快照。
