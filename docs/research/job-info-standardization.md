# BOSS 职位信息标准化研究

研究日期：2026-07-24

## 结论

第一版把候选人主动导入的 BOSS 可见职位内容标准化为“标准化职位信息”。它同时保存原始文本和结构化字段：结构化字段用于筛选、比较和排序；原始职位描述、职责和要求全文进入向量检索索引，用于解释匹配、改写简历和生成 HR 回复草稿。

标准化过程不假设 BOSS 页面字段永远完整。每个字段都应保留来源、解析状态和置信度；当详情页需要安全校验、登录或只显示部分内容时，职位信息必须标记为“不完整”，不能伪装成完整岗位详情。

## 当前可见字段

公开 BOSS 页面和搜索引擎缓存到的 BOSS 列表内容通常展示这些字段：

- 职位标题和薪资，例如 `Python AI 应用开发工程师 10-12K`
- 地点，例如 `成都`、`杭州滨江区浦沿`
- 经验要求，例如 `经验不限`、`在校/应届`、`1-3年`、`3-5年`、`5-10年`
- 学历要求，例如 `学历不限`、`大专`、`本科`、`硕士`
- 职位描述片段，通常混合岗位职责、任职要求、技术栈和补充条件
- 公司名称
- 公司行业，例如 `计算机软件`、`人工智能`、`互联网金融`
- 融资阶段，例如 `未融资`、`不需要融资`、`A轮`、`已上市`
- 公司规模，例如 `0-20人`、`20-99人`、`1000-9999人`

BOSS 首页还提供职位类型和岗位分类入口，例如后端开发、前端/移动开发、测试、运维/技术支持、人工智能、数据、产品、运营、销售等。职位类型可以作为搜索规划和岗位方向归一化的参考，但不要把它当成完整且稳定的枚举表。

## 推荐标准化模型

第一版职位信息建议分为 5 层。

### 1. 来源层

- `source_platform`: 固定为 `boss_zhipin`
- `source_url`: 候选人复制职位时所在 URL，可为空
- `captured_at`: 候选人导入时间
- `raw_text`: 候选人粘贴的原始文本
- `import_method`: `paste_text`、`manual_file`、`screenshot_ocr`
- `completeness`: `card_only`、`partial_detail`、`full_visible_detail`

### 2. 职位基础层

- `job_title_raw`
- `job_title_normalized`
- `job_category`
- `job_seniority`
- `employment_type`: `full_time`、`internship`、`part_time`、`contract`、`unknown`
- `headcount`

### 3. 比较字段层

- `city`
- `district`
- `business_area`
- `salary_min`
- `salary_max`
- `salary_months`
- `salary_unit`: `month`、`day`、`hour`
- `experience_min_years`
- `experience_max_years`
- `experience_label`
- `education_min_level`
- `education_label`
- `skills`
- `certificates`

### 4. 公司画像层

- `company_name`
- `company_industry`
- `financing_stage`
- `company_size_min`
- `company_size_max`
- `company_size_label`

### 5. 文本证据层

- `description_text`
- `responsibility_text`
- `requirement_text`
- `benefit_text`
- `raw_requirement_items`
- `uncertainty_notes`

## 解析规则

- 薪资要拆成下限、上限、薪资单位和薪资月数；`10-15K·14薪` 解析为 `10`、`15`、`month`、`14`。
- `100-200元/天` 和 `45-70元/时` 不应强行转成月薪，可以保留原单位，后续排序再决定是否做估算。
- 经验要求要拆成下限和上限；`经验不限` 解析为无硬性下限，`在校/应届` 单独保留为标签。
- 学历要求要映射为可比较等级；`学历不限 < 大专 < 本科 < 硕士 < 博士`。
- 城市字段要尽量拆成城市、区县和商圈；无法拆分时保留原始地点。
- 技能不要只依赖平台标签，因为很多技能藏在职位描述里；第一版应从职位标题、描述和要求文本中抽取技能候选项，并保留置信度。
- 岗位职责和任职要求经常混在同一段里。第一版可以先保留全文，再用规则和 LLM 生成 `responsibility_text` 与 `requirement_text` 的草稿。
- 公司规模、融资阶段、行业等字段用于偏好和排序，不应默认作为硬性淘汰条件。

## 设计影响

- 导入器必须保留原始文本，结构化结果只是可修正的解析结果。
- 标准化器输出字段级置信度和不确定说明，供职位匹配时解释。
- 职位匹配不直接读取 BOSS 页面，而读取标准化职位信息和对应长文本索引。
- 后续如果取得官方 API 或授权数据源，只需要增加新的导入适配器，仍然输出同一标准化职位模型。

## 来源

- BOSS 首页职位类型和岗位分类入口：https://www.zhipin.com/
- BOSS 职位列表页样例：https://www.zhipin.com/zhaopin/af48a95afcfd31c10XJ72t29/
- BOSS 移动端职位列表页样例：https://m.zhipin.com/zhaopin/191355afb97a2d3b1HN62N67FQ~~/
- BOSS 用户协议入口：https://about.zhipin.com/agreement
