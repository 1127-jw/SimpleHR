"""招聘工作流（LangGraph 状态图）。

节点：``screening`` / ``scoring`` / ``email_invite`` /
``get_candidate_email_reply_node`` / ``check_availability`` /
``confirm`` / ``refused`` / ``rejected`` / ``negotiation_expired`` / ``error``。

流程图
------

::

    START
      │
      ▼
    screening ──failed──▶ rejected ──▶ END          （硬性条件，纯规则不调 LLM）
      │passed
      ▼
    scoring ────failed──▶ rejected ──▶ END          （overall_score > SCORE_PASS_THRESHOLD）
      │passed
      ▼
    email_invite                                    （发面试邀请邮件 + 查面试官空闲时间）
      │
      ▼
    get_candidate_email_reply_node                  ◀── interrupt：等待候选人回复
      │
      ├── resume=TIMEOUT ────────────────────────▶ negotiation_expired ──▶ END
      │
      └── 其他回复（notify 恢复）
              │
              ▼
            check_availability                      （LLM 抽取时间 + 面试官日历校验）
              │
              ├── confirm ───────────────────────▶ confirm ──▶ END
              ├── refused ───────────────────────▶ refused ──▶ END
              └── 超时 / 需继续协商
                      │
                      ├── 轮次用尽 ─────────────▶ negotiation_expired ──▶ END
                      └── 未到上限 ─────────────▶ 回到 get_candidate_email_reply_node

外部入口（不经过 START）
------------------------

* **notify**（候选人回复邮件）：``Command(resume=邮件正文)`` → ``get_candidate_email_reply_node``
* **trigger_manual_retry**（人工通过/重试）：``Command(goto="email_invite")`` → ``email_invite``
* **sweep_stale_email_replies**（超时巡检）：``Command(resume=TIMEOUT)`` → ``negotiation_expired``

其他
----

* ``error`` 节点：任一节点抛异常时由 ``@node_error_wrapper`` 标记并流转到 ``error`` → ``END``；
* 所有终态（``rejected`` / ``confirm`` / ``refused`` / ``negotiation_expired`` / ``error``）均汇到 ``END``。
"""
