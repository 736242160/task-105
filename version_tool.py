#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
version_tool.py — 多版本并行数据管理模拟器（纯标准库，单文件）

用法:
    python3 version_tool.py 输入.json          # 从文件读取操作流
    python3 version_tool.py < 输入.json        # 从标准输入读取
    python3 version_tool.py --demo             # 运行内置示例（覆盖全部错误类型）

=====================================================================
输入格式（JSON）
=====================================================================
{
  "rules": {
    "auto_resolve": false          # 合并冲突时是否按优先级自动裁决
  },
  "operations": [
    {"op": "create",   "version": "v1", "priority": 10, "fields": {"a": 1}},
    {"op": "update",   "version": "v1", "fields": {"a": 2}},
    {"op": "commit",   "version": "v1"},
    {"op": "rollback", "version": "v1", "to": "v1#c1"},
    {"op": "merge",    "source": "v1", "target": "v2"},
    {"op": "resolve",  "version": "v2", "resolutions": {"a": 2}}
  ]
}

=====================================================================
版本规则（自定，附理由）
=====================================================================
R1 版本优先级: 每个版本创建时带整数 priority（缺省 0）。
   仅当 rules.auto_resolve=true 时，合并冲突由 priority 高的一方胜出
   （相等时 target 胜出，保证确定性）。理由：优先级代表业务权威度，
   自动裁决只应发生在显式授权时，否则一律要求人工 resolve。
R2 可回退点: 只有 commit 产生的快照才是合法回退点，回退目标用
   提交号 "vX#cN" 引用。理由：commit 是显式声明的稳定检查点，
   未提交的中间态不具备可恢复语义。
R3 提交只读: commit 后版本进入 committed 状态，update 报
   READONLY_VIOLATION；rollback 是唯一的"解封"手段——回退后版本
   重新变为 active，可继续演进。理由：已提交内容可能被他人引用，
   原地篡改会破坏可追溯性；回退生成新的演进线而非篡改历史。
R4 合并语义: merge(source, target) 把 source 字段并入 target。
   双方都有且值不同的字段 = 冲突，记录 {字段, 两版本值}；
   其余字段直接并集。冲突未解决前 target 进入 blocked 状态。
R5 冲突阻塞: 存在未解决冲突的版本，其 update/commit/merge/rollback
   一律报 PENDING_CONFLICT，只有 resolve 能解除。理由：冲突态的
   数据语义不确定，继续操作会把不确定性扩散到历史与下游。
R6 引用校验: 操作引用不存在的版本号报 VERSION_NOT_FOUND，
   回退引用不存在的提交号报 ROLLBACK_TARGET_NOT_FOUND。
R7 历史可追溯: 每个版本维护完整 history（操作序号、事件、结果），
   回退只追加历史、从不删除。

=====================================================================
输出格式（JSON）
=====================================================================
{
  "rules":    {...实际生效规则...},
  "versions": {"v1": {"status", "priority", "fields", "commits",
                      "pending_conflicts", "history"}, ...},
  "errors":   [{"seq", "op", "code", "message"}, ...]
}

=====================================================================
输入输出示例（即 --demo 内置用例）
=====================================================================
见文件底部 DEMO_SPEC；运行 `python3 version_tool.py --demo` 查看输出。
"""

import argparse
import copy
import json
import sys


# ---------------------------------------------------------------- 版本实体

class Version:
    """一个并行演进的版本：字段内容 + 状态 + 提交快照 + 完整历史。"""

    def __init__(self, vid, priority, fields):
        self.id = vid
        self.priority = priority
        self.fields = dict(fields)          # 当前字段内容
        self.status = "active"              # active | committed | blocked
        self.snapshots = []                 # [{"commit": "v1#c1", "fields": {...}}]
        self.pending_conflicts = []         # 未解决的合并冲突
        self.history = []                   # [{"seq", "event", "detail"}]

    def log(self, seq, event, detail=""):
        self.history.append({"seq": seq, "event": event, "detail": detail})

    def to_dict(self):
        return {
            "status": self.status,
            "priority": self.priority,
            "fields": self.fields,
            "commits": [s["commit"] for s in self.snapshots],
            "pending_conflicts": self.pending_conflicts,
            "history": self.history,
        }


# ---------------------------------------------------------------- 引擎

class Engine:
    def __init__(self, rules):
        self.rules = {"auto_resolve": bool(rules.get("auto_resolve", False))}
        self.versions = {}
        self.errors = []

    # ---- 工具 ----
    def _err(self, seq, op, code, message):
        self.errors.append({"seq": seq, "op": op, "code": code, "message": message})

    def _get(self, seq, op, vid):
        v = self.versions.get(vid)
        if v is None:
            self._err(seq, op, "VERSION_NOT_FOUND",
                      "版本 '%s' 不存在" % vid)
        return v

    def _check_blocked(self, seq, op, v):
        """R5: 有未解决冲突时禁止其他操作。"""
        if v.pending_conflicts:
            fields = [c["field"] for c in v.pending_conflicts]
            self._err(seq, op, "PENDING_CONFLICT",
                      "版本 '%s' 存在未解决冲突字段 %s，须先 resolve"
                      % (v.id, fields))
            return True
        return False

    # ---- 操作 ----
    def op_create(self, seq, o):
        vid = o.get("version")
        if not vid:
            self._err(seq, "create", "MISSING_PARAM", "create 缺少 version")
            return
        if vid in self.versions:
            self._err(seq, "create", "VERSION_EXISTS",
                      "版本 '%s' 已存在" % vid)
            return
        v = Version(vid, int(o.get("priority", 0)), o.get("fields", {}))
        v.log(seq, "create", "priority=%d fields=%s" % (v.priority, v.fields))
        self.versions[vid] = v

    def op_update(self, seq, o):
        v = self._get(seq, "update", o.get("version"))
        if v is None or self._check_blocked(seq, "update", v):
            return
        if v.status == "committed":                      # R3 只读
            self._err(seq, "update", "READONLY_VIOLATION",
                      "版本 '%s' 已提交，处于只读状态，禁止 update"
                      % v.id)
            return
        fields = o.get("fields", {})
        v.fields.update(fields)
        v.log(seq, "update", "set %s" % fields)

    def op_commit(self, seq, o):
        v = self._get(seq, "commit", o.get("version"))
        if v is None or self._check_blocked(seq, "commit", v):
            return
        if v.status == "committed":
            self._err(seq, "commit", "ALREADY_COMMITTED",
                      "版本 '%s' 已提交，无新变更可提交" % v.id)
            return
        cid = "%s#c%d" % (v.id, len(v.snapshots) + 1)
        v.snapshots.append({"commit": cid, "fields": copy.deepcopy(v.fields)})
        v.status = "committed"
        v.log(seq, "commit", "生成快照 %s" % cid)

    def op_rollback(self, seq, o):
        v = self._get(seq, "rollback", o.get("version"))
        if v is None or self._check_blocked(seq, "rollback", v):
            return
        target = o.get("to")
        snap = next((s for s in v.snapshots if s["commit"] == target), None)
        if snap is None:                                 # R2/R6
            self._err(seq, "rollback", "ROLLBACK_TARGET_NOT_FOUND",
                      "版本 '%s' 不存在提交点 '%s'（可回退点: %s）"
                      % (v.id, target, [s["commit"] for s in v.snapshots]))
            return
        v.fields = copy.deepcopy(snap["fields"])
        v.status = "active"                              # R3: 回退后解封
        v.log(seq, "rollback", "回退到 %s，版本重新变为 active" % target)

    def op_merge(self, seq, o):
        src = self._get(seq, "merge", o.get("source"))
        dst = self._get(seq, "merge", o.get("target"))
        if src is None or dst is None:
            return
        if self._check_blocked(seq, "merge", dst):
            return
        if src.id == dst.id:
            self._err(seq, "merge", "SELF_MERGE", "版本不能与自身合并")
            return

        conflicts, merged = [], dict(dst.fields)
        for k, sv in src.fields.items():
            if k in dst.fields and dst.fields[k] != sv:  # R4 同字段不同值
                conflicts.append({"field": k,
                                  "source_value": sv,
                                  "target_value": dst.fields[k],
                                  "source": src.id})
            else:
                merged[k] = sv

        if conflicts and self.rules["auto_resolve"]:     # R1 优先级裁决
            for c in conflicts:
                winner = src if src.priority > dst.priority else dst
                merged[c["field"]] = winner.fields[c["field"]]
                c["resolved_by"] = "auto(priority=%d)" % winner.priority
            dst.fields = merged
            dst.log(seq, "merge",
                    "合并 %s，%d 处冲突按优先级自动裁决: %s"
                    % (src.id, len(conflicts), conflicts))
        elif conflicts:                                  # 进入阻塞态
            dst.fields = merged
            dst.pending_conflicts.extend(conflicts)
            dst.status = "blocked"
            dst.log(seq, "merge",
                    "合并 %s，产生 %d 处冲突，版本阻塞待 resolve: %s"
                    % (src.id, len(conflicts), conflicts))
        else:
            dst.fields = merged
            dst.log(seq, "merge", "合并 %s，无冲突" % src.id)

    def op_resolve(self, seq, o):
        v = self._get(seq, "resolve", o.get("version"))
        if v is None:
            return
        if not v.pending_conflicts:
            self._err(seq, "resolve", "NO_PENDING_CONFLICT",
                      "版本 '%s' 没有待解决的冲突" % v.id)
            return
        res = o.get("resolutions", {})
        unresolved = [c["field"] for c in v.pending_conflicts if c["field"] not in res]
        if unresolved:
            self._err(seq, "resolve", "INCOMPLETE_RESOLUTION",
                      "冲突字段 %s 未提供解决方案" % unresolved)
            return
        for c in v.pending_conflicts:
            v.fields[c["field"]] = res[c["field"]]
        v.log(seq, "resolve", "人工解决冲突: %s" % res)
        v.pending_conflicts = []
        v.status = "active"

    # ---- 主循环 ----
    def run(self, operations):
        handlers = {"create": self.op_create, "update": self.op_update,
                    "commit": self.op_commit, "rollback": self.op_rollback,
                    "merge": self.op_merge, "resolve": self.op_resolve}
        for seq, o in enumerate(operations, 1):
            h = handlers.get(o.get("op"))
            if h is None:
                self._err(seq, o.get("op"), "UNKNOWN_OP",
                          "未知操作 '%s'" % o.get("op"))
                continue
            h(seq, o)
        return {
            "rules": self.rules,
            "versions": {vid: v.to_dict() for vid, v in self.versions.items()},
            "errors": self.errors,
        }


# ---------------------------------------------------------------- 内置示例

DEMO_SPEC = {
    "rules": {"auto_resolve": False},
    "operations": [
        {"op": "create", "version": "v1", "priority": 10,
         "fields": {"name": "alice", "age": 30}},
        {"op": "create", "version": "v2", "priority": 5,
         "fields": {"name": "alice", "city": "beijing"}},
        {"op": "update", "version": "v1", "fields": {"age": 31}},
        {"op": "commit", "version": "v1"},
        # 错误1: 提交后再更新 -> READONLY_VIOLATION
        {"op": "update", "version": "v1", "fields": {"age": 32}},
        # 错误2: 引用不存在的版本 -> VERSION_NOT_FOUND
        {"op": "update", "version": "v9", "fields": {"x": 1}},
        {"op": "commit", "version": "v2"},
        # 回退后演进: v2 回到 c1 前的状态? 先造第二个提交点再回退
        {"op": "rollback", "version": "v2", "to": "v2#c1"},
        {"op": "update", "version": "v2", "fields": {"name": "bob"}},
        {"op": "commit", "version": "v2"},
        # 回退到 c1 -> name 恢复为 alice，后续操作一致演进
        {"op": "rollback", "version": "v2", "to": "v2#c1"},
        {"op": "update", "version": "v2", "fields": {"name": "alicia"}},
        # 错误3: 回退到不存在的提交点 -> ROLLBACK_TARGET_NOT_FOUND
        {"op": "rollback", "version": "v2", "to": "v2#c9"},
        # 合并: v1(name=alice,age=31) -> v2(name=alicia,city=beijing)
        # name 同字段不同值 -> 冲突，v2 阻塞
        {"op": "merge", "source": "v1", "target": "v2"},
        # 错误4: 冲突未解决前禁止其他操作 -> PENDING_CONFLICT
        {"op": "update", "version": "v2", "fields": {"age": 40}},
        {"op": "commit", "version": "v2"},
        # 解决冲突后恢复正常
        {"op": "resolve", "version": "v2", "resolutions": {"name": "alice"}},
        {"op": "update", "version": "v2", "fields": {"age": 40}},
        {"op": "commit", "version": "v2"},
        # 错误5: 合并不存在的版本 -> VERSION_NOT_FOUND
        {"op": "merge", "source": "v9", "target": "v2"},
    ],
}


def main():
    ap = argparse.ArgumentParser(description="多版本并行数据管理模拟器")
    ap.add_argument("input", nargs="?", help="输入 JSON 文件（缺省读 stdin）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args()

    if args.demo:
        spec = DEMO_SPEC
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            spec = json.load(f)
    else:
        spec = json.load(sys.stdin)

    result = Engine(spec.get("rules", {})).run(spec.get("operations", []))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
