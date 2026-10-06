# Skills（技能包）

本目录存放 Agent 可加载的**技能包**。一个技能 = 一个子目录，包含至少一个 `SKILL.md`：

```
skills/
└── my-skill/
    ├── SKILL.md          # 技能描述与使用说明（必需）
    └── ...               # 技能自带的脚本 / 模板等资源
```

- Agent 通过内置工具 `use_skill` 按名调用技能；技能内容会按需注入上下文
- 在 `config.yaml` 的 `skills.extra_skills_dirs` 里可追加额外技能目录（逗号分隔路径）
- 技能目录会被注入给 agent 的只有名字与 SKILL.md 内容，按最小权限原则自行把控内容

仓库内置技能（按需 `use_skill` 加载，不占常驻 token）：

- `create-waker`：把聊天需求落成数字员工 / 流程
- `file-ops`：用 bash 读写改文件（文件工具退役后的操作指引）
- `install-mcp`：用户丢来 MCP 链接时调研 README 并 `create_mcp` 接入
