# create_waker 完整示例

用户说："每天早上 9 点帮我看下项目 TODO 和 README，把进度写成日报。"

推断 + 追问后（假设用户确认输出到 daily_report.md、只读不改），调用：

```json
{
  "name": "daily-report",
  "description": "每天早上汇总项目进度，产出简明日报",
  "task_prompt": "读取工作区 README.md 与 TODO.txt，把进度日报写入 daily_report.md。格式：不超过 10 行，分「已完成 / 进行中 / 风险」三节，只依据文件内容，不执行命令，不改动源文件。",
  "schedule_type": "daily",
  "daily_at": "09:00",
  "permission_mode": "full_access",
  "identity": "项目进度日报员：每天早上巡检项目文件，产出简明进度日报。",
  "persona": "简洁、数据优先、不加主观评价。",
  "bible": "绝不修改源文件；内容只来自工作区文件，不编造。"
}
```

`permission_mode: full_access` 是因为任务要**写文件**且无人值守——
before_changes 会让每次运行卡在审批上以 error 结束。 bible 里"绝不修改
源文件"是行为约束，不是权限替代品。

创建成功后的回复要点：

1. 告知已创建、**当前未启用**，调度是"每天 09:00"。
2. 复述 task_prompt 关键内容（用户要为它审批，须让它看得懂）。
3. **明确告知该员工将以完全访问权限自动运行**（用户须知情）。
4. 问："要现在启用吗？" —— 用户同意才调 `set_waker_enabled("daily-report", true)`。

反例（不要这样）：

- task_prompt 只写"写日报"——员工不知道读什么、写到哪、什么标准。
- 写文件的任务用默认 `before_changes` 创建——无人值守卡审批，每次运行都失败。
- 用户没确认就直接 set_waker_enabled。
- 名字用中文或带空格（会校验失败，浪费一轮）。
