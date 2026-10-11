# Sluice

Excel/CSV → 模板转换框架。

把**任意来源的 Excel/CSV** 转成**你给定模板**的输出,并且证明转对了。

三条原则:

1. **运行时是确定性的。** 转换由一份 YAML spec(数据,不是代码)驱动,同样输入永远同样输出。
2. **spec 由人确认。** 规则先起草,剩下的问题交给人或聊天 LLM 回答;LLM 只看表头、几个样例值和一行示例,**从不接触文件**。
3. **校验当裁判。** 每一次删行都有记录,控制总数要对得上(`来源 − 丢弃 = 输出`),失败就不写输出,只写结构化的错误报告。

## 现状(请先读,别高估)

| 能力 | 状态 |
|---|---|
| 跑已有 spec(`run` / `run-dir`) | 可用,`sluice/specs/` 有 2 份真实示例 |
| 为新格式起草 spec | 规则起草**单靠规则通过率很低**(盲测 8 个公网文件:冻结规则 0/8,改通用机制后仍 0/8);加人工答案后 8/8 |
| LLM 起草 | 复制粘贴方式(聊天窗口,无需 API)已验证;API 方式只有接口,没跑过真模型 |
| 先例库(复用已确认的答案) | 可用,模拟下一年文件 13/20 错字段 → 0 |
| 面向最终用户的命令行壳(draft→review→approve→run 一条龙) | **还没有**,现在通过 `benchmark/` 下的脚本走 |

结论:引擎和校验可信;"自动起草"目前是"规则 + 人/LLM 答题"的半自动。详细数据见 `sluice/README.md` 与 `sample/etl_blind_holdout_20261011/README.md`。

## 安装

Python 3.11+(开发用 3.13,Windows 下用 `py`)。

```bash
pip install -r requirements.txt
py -m sluice test          # 48 个核心测试(另有 36 个需要样例包,没有样例包时自动跳过;有样例包时共 84 个,约 2 分钟)
```

`.ods` 输入需要 `odfpy`(未列入 requirements)。

## 三种用法

### A. 用现成 spec 转一个文件

```bash
py -m sluice run sluice/specs/dhl_de_transport.yaml sluice/tests/fixtures/transport/input/DHL_DE_202604_Transport.xlsx --out 输出目录 --template sluice/tests/fixtures/transport/transport_template.xlsx
py -m sluice dry-run  SPEC 输入.xlsx --template T.xlsx     # 只看步骤轨迹,不写文件
py -m sluice run-dir  文件夹 --template T.xlsx              # 按文件名匹配 spec
```

失败时不写输出,看 `_etl_logs/<run>.feedback.md`(人话版原因)和 `dq_logs/issues.csv`。

### B. 为新格式起草 spec(表头级模板)

```bash
py -m sluice draft 新文件.xlsx --template 目标模板.csv --key 列1,列2 --filename 输出名.csv --out spec.yaml
```

做不了的部分会作为"问题"列出来,不会悄悄猜。人看过再 `run`。

### C. 记录型模板(模板里有一行示例)+ 聊天 LLM 起草

这是更通用的路线:目标是"字段名 + 类型 + 规则说明 + 1 行示例"。流程:

1. 生成提示词(只含结构,不含文件):
   `py benchmark/realworld/prompt.py dump <case>`
2. 粘到聊天窗口,拿回 JSON 答案,存成文件。
3. 校验并评分:
   `py benchmark/realworld/prompt.py apply <case> --reply "<sheet>=答案.txt"`
   会打印**复核清单**(范围过滤保留/丢弃了什么、从示例复制的常量、低置信度规则猜测),风险高的在前面。
4. 确认无误后存入先例库,下次同结构文件不用再问:
   `... --store s.json --save --by 你的名字 [--confirm "理由"]`

目前这些命令是以样例包的 case 为单位写的(`benchmark/realworld/rw.py`);接你自己的文件需要写一个 case 描述,可参考 `benchmark/realworld/` 与 `sample` 里的 manifest 格式。**这一层还没有产品化,是下一步要做的事。**

## 目录

```
sluice/            引擎、起草、复核、先例库、测试(核心,详细文档见 sluice/README.md)
  engine/          locate → map → shape(ops) → keys → template → controls → write
  infer/           profile(读结构)、frame、columns、bindings、compile、blind(起草)、precedent_store
  agent/           LLM 接口:提示词、响应校验、复核、自愈(只允许改"怎么读",不改控制值)
  specs/           2 份真实 spec 示例
  tests/fixtures/  测试用的小型合成运输文件
benchmark/         评测与复制粘贴工具(realworld/ blind/ external/ specs/)
sample/            只保留盲测的来源记录、冻结哈希、各轮结果和构建脚本;数据本身不在仓库里
```

## 样例包

评测用的公开数据包(约 270 MB)不在仓库里。需要时:

* `etl_realworld_noisy_20261011_v3`:`python sample/.../fetch_originals.py` 重新下载 20 个案例。
* `etl_blind_holdout_20261011`:来源 URL 与 SHA-256 在 `SOURCES.json`,模板/期望输出由 `tools/build_holdout.py` 生成。
* 跑评测:`py benchmark/realworld/rw.py run all --human`(需要先有对应样例包)。

## 盲测规则(想自己验证泛化时)

先冻结代码哈希,期望输出由不导入本仓库代码的独立脚本生成;第一轮不加任何答案;之后的修复要如实标注为"非盲测"。示例见 `sample/etl_blind_holdout_20261011/frozen.json`。

## 贡献约定

* 新能力以"绑定种类"(`sluice/infer/bindings.py`)或"ops"(`@register_op`)加入,**必须配测试**,并且在 `compile` 之前验证。
* 不要为了某个样例在规则里写死特例;用 `answers/` 或先例库表达人的判断。
* 改了起草规则后跑 `py -m sluice test` 和 `py benchmark/realworld/rw.py run all --human` 看回归。
