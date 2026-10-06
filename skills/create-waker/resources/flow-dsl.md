# WakerFlow DSL 速查

一个 flow = 一个 YAML 文件，顶层字段：

```yaml
name: my-flow              # ^[a-zA-Z0-9_-]{1,64}$，且不能下划线开头
description: 一句话说明
inputs:                    # 可选：运行时参数声明
- name: repo
  type: string             # string / number / boolean
  required: true
  # default: xxx           # 可选默认值
  # enum: [a, b]           # 可选枚举
steps:                     # 顶层步骤顺序执行
- id: step1
  worker: some-waker       # 引用已存在的 waker 名
  task: 做什么，可用 {{inputs.repo}} 和 {{steps.其他步id.result}}
returns:                   # 可选：运行结果汇总
  final: '{{steps.step1.result}}'
```

## 调度段（可选，与 waker 同款）

```yaml
enabled: true
schedule_type: daily       # none / interval / daily
daily_at: "09:00"
# interval_minutes: 120
```

不写调度段 = 仅手动触发。**带调度的 flow 保存后即会自动运行**，
创建前务必向用户复述调度计划。

## 五种节点

```yaml
steps:
# 1) worker：单个数字员工干活
- id: draft
  worker: report-writer
  task: 写今天的日报
  # tools: [bash]                       # 可选：收窄该步工具白名单
  # permission_mode: plan               # 可选：覆盖权限模式
  # if: "{{steps.check.result}}"        # 可选：条件执行（非空为真）

# 2) parallel：并行分支，全部完成才继续
- id: parallel
  parallel:
  - id: a
    worker: watcher-1
    task: 巡检 A
  - id: b
    worker: watcher-2
    task: 巡检 B

# 3) pipeline：串行链，上一步输出可被引用
- id: pipeline
  pipeline:
  - id: collect
    worker: collector
    task: 收集数据
  - id: summarize
    worker: summarizer
    task: 汇总：{{steps.collect.result}}

# 4) ask_user：中途向用户提问（审批页作答）
- id: confirm
  ask_user:
    question: 要发布吗？
    options:
    - {label: 发布, value: "yes"}
    - {label: 先不发, value: "no"}
    # timeout: 86400
    # default: "no"

# 5) action：HTTP 调用
- id: notify
  action:
    method: POST
    url: https://example.com/hook
    # headers: {...}
    body:
      text: '{{steps.summarize.result}}'
```

## 规则与坑

- `id` 全 flow 唯一；`worker` 引用的 waker 必须已存在（create_wakerflow
  保存时会提示缺谁，但运行到缺失步骤会失败）。
- 模板占位：`{{inputs.名}}`、`{{steps.步id.result}}`。引用不存在的
  步 id 会在校验期被拒。
- 越复杂的 flow 越要先和用户对齐步骤图，再一次性给出完整 YAML。
