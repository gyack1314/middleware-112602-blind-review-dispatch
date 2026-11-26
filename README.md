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

## 复测调度中间件

`retest_scheduler/` 是协调评测包发放、独立签收与分歧仲裁的本地中间件，
`retest_cli.py` 是持续投喂的项目入口；所有状态保存在传入的本地状态目录，
只使用合成对象与标准库，支持 `python3 -I` 隔离模式。

### 业务规则落点

- **独立双签**：机构登记 `affinity`，同一关联组的不同工号领取同一项目会被
  `affinity_conflict` 拒绝；两个不同关联组就同一证据版本给出相容结论才出证。
- **共享份额**：份额按机构声明、按关联组共享；只有真实授出（领取）才占用，
  提交、撤回、过期后释放，跨观察项目统一计数。
- **盲审隔离**：分歧后仲裁者只收到匿名 `party_alpha/party_beta` 与同一证据，
  回执与 `inspect --lab` 永不出现其他机构身份；无分歧时不能领 `decide`，
  仲裁者必须独立于争议两方关联组，在途仲裁席位不可重复领取。
- **证据更正**：相同 case/item 的更大 evidence 触发更正——未提交 review 迁移到
  新版本并换新授出编号，旧编号标记 `migrated` 保留可解释；已提交旧结果只归旧
  版本，不能参与新版出证；在途仲裁案 `abandoned`、仲裁任务撤回，无关项目不动。
- **截止与退出**：截止后提交被 `late_submission` 拒绝，清扫为 `expired` 并释放
  份额供替补；机构退出撤回其在途任务但保留已提交结果，退出只影响新领取；
  仲裁者退出后争议案回到可领取状态，旧结果晚报不能冒充替补。
- **可解释阻塞**：凑不齐两个独立群体时，`inspect --case` 的 `blockers` 区分
  欠能力（`skill_unsupported`）、关联冲突（`no_independent_group`）与份额占满
  （`lab_slots_full` / `affinity_slots_full`）。
- **持久化**：命令只产出事件，中间件先把事件 fsync 到 `journal.jsonl` 再作用于
  内存；重启完整重放日志（`snapshot.json` 仅作加速缓存），已占份额与盲审隔离
  在异常退出后保持一致；`request` 领取键幂等，重放不重复授出。

### 入口命令

    # 持续投喂一个或多个演练文件，状态累积在状态目录；规则拒绝进 error 回执，不中断
    python3 retest_cli.py run <状态目录> examples/affinity-and-disagreement.jsonl
    python3 retest_cli.py run <状态目录> 场景A.jsonl 场景B.jsonl -o 回执.jsonl --strict

    # 委托方查询：机构只看自己的包；case/总览看进度、阻塞与等待项
    python3 retest_cli.py inspect <状态目录> --lab M
    python3 retest_cli.py inspect <状态目录> --case c

    # 审计某次出证引用了哪几项有效独立决定（含关联组与确切证据版本）
    python3 retest_cli.py trace <状态目录> 1

    # 导出持久化事件日志
    python3 retest_cli.py journal <状态目录>

服务异常退出后再次运行同一条 `run` 命令即可从状态目录恢复；`restart` 消息会
额外落一个快照点。中间件自身的规则测试：

    python3 test_retest_scheduler.py          # 也可加 -I

