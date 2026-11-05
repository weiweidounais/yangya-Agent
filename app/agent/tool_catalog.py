"""Reviewed bilingual selection guidance and execution budgets for public tools.

Rows are explicit product contracts: description | use | do not use | seconds.
"""
_ROWS = {
'schedule.create': ('生成定时任务确认卡，用户确认后创建。 Propose a personal schedule for user approval.', '建立一次性提醒或自定义只读指令日程。 Use for reminders or custom read-only schedules.', '时间或内容不明确时先追问；确认卡不代表已创建。 Clarify missing time or content; a proposal is not a saved schedule.', 30),
'schedule.list': ('查看本人定时任务及运行次数。 List the current user’s schedules and run counts.', '用户想查看有哪些定时任务时使用。 Use when the user asks about their scheduled tasks.', '不得查询他人任务或把列表当成运行结果。 Never list another user’s tasks or claim execution from a listing.', 30),
'schedule.update': ('生成修改定时任务的确认卡。 Propose changes to an owned schedule for approval.', '用户要求修改明确任务的时间或内容时使用。 Use to propose a time or content change to a specified task.', '目标不唯一时先追问；确认前不得声称已修改。 Clarify ambiguous targets; do not claim changes before approval.', 30),
'schedule.delete': ('生成删除定时任务的确认卡。 Propose deletion of an owned schedule for approval.', '用户要求删除一个明确的任务时使用。 Use when the user asks to delete a specific personal task.', '不得猜测删除目标或绕过确认卡。 Never guess which task to delete or bypass user approval.', 30),
'schedule.pause': ('冻结本人指定的定时任务。 Pause a specified schedule owned by the current user.', '用户明确指定需要冻结的任务时使用。 Use when the user clearly identifies a task to pause.', '目标不明确时先追问；不得冻结他人任务。 Clarify ambiguous targets and never pause another user’s task.', 30),
'schedule.resume': ('恢复本人指定的定时任务。 Resume an owned schedule within the active task cap.', '用户明确指定需要恢复的任务时使用。 Use when the user clearly identifies a task to resume.', '不得绕过活跃任务上限或恢复他人任务。 Never bypass the active schedule cap or resume another user’s task.', 30),
'schedule.run_now': ('立即运行本人启用的定时任务。 Run an active schedule once for the current user.', '用户明确要求立即执行指定任务时使用。 Use when the user explicitly requests an immediate run.', '不得运行冻结或已删除的任务；无绑定不声称渠道送达。 Never run inactive tasks or claim channel delivery without a binding.', 30),

'profile_read': ('读取用户已保存的个人档案。 Read the saved personal profile.', '需要核对已有姓名、城市、偏好等事实时使用。 Use to retrieve known personal facts.', '不要用来修改档案。 Do not save profile changes.', 5),
'profile_save': ('整体保存用户的个人档案内容。 Replace the stored personal profile.', '已取得完整且核对过的档案时使用。 Use with a complete reviewed profile.', '仅修改个别字段时应使用合并更新。 Do not replace a profile for a small patch.', 10),
'profile_update': ('合并字段并保存用户个人档案。 Merge a patch into the saved profile.', '用户补充或修正档案字段时使用。 Use for incremental profile corrections.', '不要用来覆盖完整档案或推断事实。 Do not invent facts or replace all fields.', 10),
'draft_text': ('根据用户的写作请求和档案生成一版草稿。 Draft an email, message or article for the confirm card.', '用户要求写、改、润色或翻译一段文字时使用。 Use when the user asks for a piece of writing.', '不会发送邮件或消息；草稿必须经用户确认。 Never sends anything; drafts need user confirmation.', 90),
'memory_context': ('组合档案和相关事件供对话参考。 Format profile and relevant events as context.', '回答前需要用户已有背景时使用。 Use to assemble relevant personal context.', '不会保存记忆，也不是完整历史导出。 Do not write memory or export full history.', 10),
'memory_write': ('追加用户的事件及摘要记忆。 Append an event with a summary.', '需要保留已发生的重要事件时使用。 Use to retain a completed event.', '不要记录密码或把推测写成事实。 Do not store secrets or unverified claims.', 10),
'memory_search': ('按关键词、标签和线程检索事件记忆。 Search events by keywords, tags and thread.', '需要寻找与当前问题有关的历史事件时使用。 Use to recall matching events.', '不是向量搜索，也不会修改任何记忆。 Do not use for semantic vector search or edits.', 5),
'memory_facts_search': ('查记忆：混合检索当前用户的有效事实，返回 confirmed 标记。 Search current-user facts with confirmation status.', '用户询问以前说过的偏好、计划或事情时使用。 Use to recall personal facts.', 'confirmed=false 只能核实，不得作为用户确认的事实；不能搜索他人记忆。 Never assert pending inferences or search another user.', 7),
'memory_get': ('通过事件编号读取一条记忆记录。 Retrieve one memory event by its identifier.', '已知事件编号且需要查看详情时使用。 Use to inspect an already identified event.', '没有事件编号时应先搜索记忆。 Do not use for broad history search.', 5),
'memory_update': ('更新本人指定事件的摘要内容。 Replace the summary of an owned memory event.', '用户指出某条事件摘要有误时使用。 Use to correct a specific event summary.', '不要用来修改档案事实或新增事件。 Do not edit profile facts or append events.', 10),
'memory_delete': ('删除本人指定的一条事件记忆。 Delete a single owned memory event.', '用户明确要求移除指定事件时使用。 Use for explicit deletion of one event.', '不要用于清空全部记忆或删除档案。 Do not erase all history or profile facts.', 10),
'memory_delete_all': ('删除用户全部事件记录但保留档案。 Delete all episodic events for the user.', '用户要求清除事件历史时使用。 Use when all event history should be removed.', '要同时忘记档案时应使用全部遗忘流程。 Do not use for full profile erasure.', 20),
'memory_card': ('生成可查看和编辑的个人记忆卡片。 Build a memory card of facts and events.', '用户询问记住了什么或准备编辑记忆时使用。 Use to inspect remembered facts and events.', '不会自动更改卡片中的事实和事件。 Do not use to apply memory edits.', 10),
'memory_fact_update': ('通过记忆卡修改指定档案事实。 Update an identified profile fact from the memory card.', '用户纠正城市、偏好等已展示事实时使用。 Use to correct a displayed profile fact.', '事件摘要修改应使用事件编辑工具。 Do not use for episodic summary edits.', 10),
'memory_fact_delete': ('通过记忆卡删除指定档案事实。 Remove an identified fact from the profile.', '用户明确要求忘记某项档案信息时使用。 Use to forget one specified profile fact.', '不会删除事件历史或清空整个档案。 Do not erase events or the entire profile.', 10),
'memory_episode_update': ('编辑记忆卡中的事件并刷新记忆版本。 Edit a card event and update memory revision.', '用户在记忆卡上纠正某条事件时使用。 Use for a correction from an event card.', '不要用来改结构化档案字段。 Do not edit structured profile facts.', 10),
'memory_episode_delete': ('删除记忆卡中的事件并刷新记忆版本。 Delete a card event and update memory revision.', '用户从记忆卡明确删除一条事件时使用。 Use for deletion from an event card.', '不要用来清空所有事实和全部事件。 Do not erase the entire memory collection.', 10),
'memory_forget': ('经明确确认后清除用户档案和事件记忆。 Erase profile and event memory after confirmation.', '用户明确选择全部遗忘并确认时使用。 Use after explicit confirmation to forget everything.', '只纠正或删除单条记忆时不要使用。 Do not use for a single correction or deletion.', 30),
'summarize_turn': ('把一轮对话加入后台摘要任务队列。 Enqueue a conversation turn for background summarization.', '一轮对话结束且需要保留摘要时使用。 Use after a turn worth summarizing.', '不会立即返回摘要，不用于读取历史。 Do not expect an immediate summary or history read.', 10),
'proactive.deliver': ('向本人已绑定的渠道发送一条提醒。 Deliver one reminder to the owner’s bound channel.', '提醒已准备好且收件渠道属于当前用户时使用。 Use for a prepared reminder to a verified owner channel.', '不能向未绑定渠道或其他用户发送。 Do not deliver to unbound channels or other users.', 20),

'sport.profile_get': ('读取用户的运动档案：目标、水平、每周天数、单次时长、伤病、器械。 Read the saved sport profile.', '排计划或需要确认用户目标与限制时使用。 Use before planning or when goals and limits matter.', '不会返回其他用户档案，也不能代替训练记录。 Never read another user or substitute for the workout log.', 10),
'sport.profile_update': ('合并保存用户的运动档案。 Merge sport profile fields.', '用户明确说出目标、水平、每周天数、单次时长、伤病或器械时使用。 Use when the user states those facts.', '不要推测伤病或能力；空缺字段保持原值。 Do not infer injuries or ability; empty fields keep old values.', 10),
'sport.log_workout': ('记录一次训练：项目、时长、距离、强度、备注。 Append one workout session.', '用户说练完或描述了已完成训练时使用。 Use after the user reports a finished session.', '只记录已发生的训练，不把计划当成已完成。 Log only real sessions, never a planned one.', 10),
'sport.history': ('汇总本人一段时间的训练次数、时长、距离与连续天数。 Summarise the owner’s workouts over a window.', '用户问练了多少、这周练了几次时使用。 Use for volume and frequency questions.', '没有记录时如实说没有，不得估算。 Say there is no log instead of estimating.', 10),
'sport.plan_get': ('读取当前保存的训练计划。 Read the current weekly plan.', '用户问计划内容或准备调整计划时使用。 Use to review or adjust the plan.', '没有计划时不要临时编一份。 Do not invent a plan when none is saved.', 10),
'sport.plan_save': ('保存用户的训练计划与每周安排。 Save the weekly training plan.', '已和用户确认目标、周次与安排后使用。 Use after the goal, weeks and weekly items are agreed.', '不得把未确认的时间或强度写进计划。 Never store unconfirmed times or intensities.', 20),

'companion.set_body': ('保存身高、体重、体脂等身体数据并创建搭子。 Save body metrics and create the companion.', '用户首次使用或主动更新身体数据时使用。 Use on first setup or when the user updates body metrics.', '不得猜身高体重或诊断肥胖；缺失就追问。 Never guess metrics or diagnose; ask when missing.', 10),
'companion.status': ('查看搭子状态、今日热量差与体重方向。 Read the companion state and today’s energy balance.', '用户问搭子怎么样、今天会不会胖时使用。 Use for status, mood and weight-direction questions.', '数据缺失时先引导填写；不得声称能预测真实体重。 Do not predict real weight; ask for missing metrics.', 10),
'companion.log_meal': ('记录一次饮食及其热量。 Log one meal or snack with calories.', '用户说吃了什么时使用；估算热量需标注为估算。 Use when the user reports food; label estimates.', '不得编造精确重量或热量；不确定时追问。 Never invent precise calories; ask instead.', 10),
'companion.meal_suggest': ('按剩余热量与目标给三个健康餐选项。 Suggest three healthy meal options.', '用户不知道吃什么时使用。 Use when the user asks what to eat.', '不作医疗或疾病饮食建议；不保证减重。 No medical diets or weight promises.', 10),
'companion.recap': ('汇总一段时间的运动、饮食与体重变化。 Summarise activity, intake and weight change.', '用户要阶段总结时使用。 Use for periodic reviews.', '不得把缺失数据当成零；说明没有记录。 Do not treat missing data as zero.', 10),
'companion.set_style': ('修改搭子名字或社交风格。 Rename the companion or set its social style.', '用户给搭子起名或纠正性格标签时使用。 Use when the user names the companion or corrects a label.', '性格标签只用于说话方式，不得用于诊断或对外展示。 Never diagnose or expose the label to others.', 10),
'companion.rest_timer': ('生成组间休息计时卡并给一条动作提示。 Build a rest timer card.', '用户说组间休息、下一组或需要计时時使用。 Use between sets or when timing a rest.', '不替代教练或医生；不承诺动作一定标准。 Not coaching certification or medical advice.', 10),
'companion.weekly_report': ('生成本周搭子周报文本。 Build this week’s companion report.', '用户要看周报或阶段回顾时使用。 Use for a weekly review.', '不得编造缺失数据；没有记录就直说。 Never invent missing data.', 20),
'companion.revive': ('唤醒沉睡的搭子，进入下一代。 Revive the companion as a new generation.', '用户明确要叫醒搭子时使用。 Use only when the user asks to revive it.', '不得自动唤醒或隐瞒已沉睡的事实。 Never auto-revive or hide dormancy.', 10),
}


def policy(name, risk):
    row = _ROWS.get(name)
    if row is None:
        return {}
    description, when, avoid, timeout = row
    return dict(description=description, when_to_use=when, when_not_to_use=avoid,
                timeout=timeout, max_retries=1 if risk == 'read' else 0)
