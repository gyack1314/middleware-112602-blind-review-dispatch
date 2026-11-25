# 机器人能力复测的盲派单与跨机构仲裁

观察项目用 case/item 标识，证据版本 evidence。affinity 指不具独立性的业务组。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **lab**　登记 lab、affinity、skills、slots 与 active。
- **packet**　case/item、skill、evidence 和 deadline。相同 case/item 更新 evidence 表示更正。
- **claim**　lab 按 case/item 申请 role，role 为 review 或 decide，request 为领取键。
- **submit**　lab 按 assignment 给出 evidence 和 verdict。assignment 是本项目每次真实授出的 1 起编号。 verdict 是合成结论 pass 或 return，决定方只在有效结论有分歧时领取。
- **revoke**　运营撤销指定未完成的 assignment。
- **restart**　模拟派单服务重启。
- **inspect**　按 lab 或 case 查状态。

## 示例

- affinity-and-disagreement.jsonl
- revision-overtakes-adjudication.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。
