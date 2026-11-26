"""HTTP routes: server-rendered drill console + JSON API."""
from __future__ import annotations

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, url_for)

from .examples import EXAMPLES
from .model import normalize_traffic
from .netutils import ValidationError, parse_ipv4

bp = Blueprint("web", __name__)


def service():
    return current_app.extensions["drill_service"]


# ======================================================================
# Pages
# ======================================================================

@bp.get("/")
def dashboard():
    svc = service()
    active = svc.active_revision()
    form_model = _default_form_model()
    return render_template(
        "dashboard.html",
        active=active,
        examples=EXAMPLES,
        form_model=form_model,
        field_errors={},
        result=None,
        status_code=200,
    )


@bp.post("/drills")
def submit_drill_form():
    svc = service()
    form_model = _form_model_from_form(request.form)
    payload = _drill_payload_from_form(request.form)

    try:
        result = svc.submit_drill(payload)
    except ValidationError as exc:
        field_errors = _issue_map(exc.issues)
        return render_template(
            "dashboard.html", active=svc.active_revision(),
            examples=EXAMPLES, form_model=form_model,
            field_errors=field_errors, validation=exc.to_dict(),
            result=None), 400

    if result.get("ok"):
        return redirect(url_for("web.revision_detail",
                                revision_id=result["revision_id"], new=1))

    status = {"no_solution": 422, "invalid_rules": 422,
              "publish_failed": 502}.get(result.get("code"), 400)
    return render_template(
        "dashboard.html", active=svc.active_revision(),
        examples=EXAMPLES, form_model=form_model,
        field_errors={}, validation=None, result=result), status


@bp.get("/rules")
def rules_page():
    svc = service()
    rev_id = request.args.get("revision_id", type=int)
    rev, snapshot = _resolve_revision(svc, rev_id)
    revisions = svc.storage.list_revisions()
    return render_template("rules.html", rev=rev, snapshot=snapshot,
                           revisions=revisions)


@bp.get("/replay")
def replay_page():
    svc = service()
    rev_id = request.args.get("revision_id", type=int)

    run = None
    run_id = request.args.get("run", type=int)
    if run_id:
        run = svc.storage.get_replay_run(run_id)
        # A run is always rendered against the whole revision snapshot it used.
        if rev_id is None and run is not None:
            rev_id = run["revision_id"]

    rev, snapshot = _resolve_revision(svc, rev_id)
    recent_runs = svc.storage.list_replay_runs()
    revisions = [r for r in svc.storage.list_revisions()
                 if r["status"] == "published"]
    return render_template(
        "replay.html", rev=rev, snapshot=snapshot, run=run,
        trial=None, trial_error=None, recent_runs=recent_runs,
        revisions=revisions, send_form={"source": "", "group": ""})


@bp.post("/replay/run")
def replay_run_form():
    svc = service()
    rev_id = request.form.get("revision_id", type=int)
    result = svc.run_replay(revision_id=rev_id)
    if not result.get("ok"):
        rev, snapshot = _resolve_revision(svc, rev_id)
        return render_template(
            "replay.html", rev=rev, snapshot=snapshot, run=None,
            trial=None, trial_error=result.get("message"),
            recent_runs=svc.storage.list_replay_runs(),
            revisions=_published_revisions(svc),
            send_form={"source": "", "group": ""}), 409
    return redirect(url_for("web.replay_page", run=result["run_id"],
                            revision_id=result["revision_id"]))


@bp.post("/send")
def trial_send_form():
    svc = service()
    rev_id = request.form.get("revision_id", type=int)
    raw_src = (request.form.get("source") or "").strip()
    raw_grp = (request.form.get("group") or "").strip()
    send_form = {"source": raw_src, "group": raw_grp}

    trial = None
    trial_error = None
    try:
        source = parse_ipv4(raw_src, "源地址")
        group = parse_ipv4(raw_grp, "组地址")
    except ValueError as exc:
        trial_error = str(exc)
    else:
        trial = svc.trial_send(source, group, revision_id=rev_id)
        if not trial.get("ok"):
            trial_error = trial.get("message")

    rev, snapshot = _resolve_revision(
        svc, trial.get("revision_id") if trial and trial.get("revision_id") else rev_id)
    run_id = request.args.get("run", type=int)
    run = svc.storage.get_replay_run(run_id) if run_id else None
    return render_template(
        "replay.html", rev=rev, snapshot=snapshot, run=run,
        trial=trial, trial_error=trial_error,
        recent_runs=svc.storage.list_replay_runs(),
        revisions=_published_revisions(svc),
        send_form=send_form)


@bp.get("/revisions")
def revisions_page():
    svc = service()
    revisions = svc.storage.list_revisions(limit=100)
    active = svc.active_revision()
    return render_template("revisions.html", revisions=revisions,
                           active_id=active["id"] if active else None)


@bp.get("/revisions/<int:revision_id>")
def revision_detail(revision_id: int):
    svc = service()
    rev = svc.storage.get_revision(revision_id)
    active = svc.active_revision()
    if rev is None:
        return render_template("not_found.html"), 404
    return render_template("revision_detail.html", rev=rev,
                           active_id=active["id"] if active else None,
                           just_published=request.args.get("new") == "1")


# ======================================================================
# JSON API
# ======================================================================

@bp.get("/healthz")
def healthz():
    svc = service()
    try:
        active = svc.active_revision()
        return jsonify(status="ok",
                       active_revision_id=active["id"] if active else None)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify(status="error", error=str(exc)), 500


@bp.get("/api/examples")
def api_examples():
    return jsonify(EXAMPLES)


@bp.get("/api/config")
def api_config():
    svc = service()
    rev_id = request.args.get("revision_id", type=int)
    rev, snapshot = _resolve_revision(svc, rev_id)
    if snapshot is None:
        return jsonify(ok=False, code="not_found",
                       message="没有已发布配置"), 404
    return jsonify(ok=True, revision_id=rev["id"], revision=rev,
                   snapshot=snapshot)


@bp.post("/api/drills")
def api_submit_drill():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, code="invalid_input",
                       error={"message": "请求体必须是 JSON 对象"}), 400
    svc = service()
    try:
        result = svc.submit_drill(data)
    except ValidationError as exc:
        return jsonify(ok=False, code="invalid_input",
                       error=exc.to_dict()), 400
    if result.get("ok"):
        return jsonify(result), 201
    status = {"no_solution": 409, "publish_failed": 502,
              "invalid_rules": 422}.get(result.get("code"), 400)
    return jsonify(result), status


@bp.post("/api/replay")
def api_replay():
    body = request.get_json(silent=True) or {}
    rev_id = body.get("revision_id")
    if rev_id is not None:
        try:
            rev_id = int(rev_id)
        except (TypeError, ValueError):
            return jsonify(ok=False, code="invalid_input",
                           error={"message": "revision_id 必须是整数"}), 400

    override_flows = None
    if body.get("flows") is not None:
        try:
            override_flows, issues = normalize_traffic(body["flows"])
        except ValidationError as exc:
            return jsonify(ok=False, code="invalid_input",
                           error=exc.to_dict()), 400
        if issues:
            return jsonify(ok=False, code="invalid_input",
                           error={"message": "流量行校验失败",
                                  "issues": issues}), 400

    result = service().run_replay(revision_id=rev_id,
                                  override_flows=override_flows)
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)


@bp.post("/api/send")
def api_send():
    body = request.get_json(silent=True) or {}
    try:
        source = parse_ipv4(body.get("source"), "源地址")
        group = parse_ipv4(body.get("group"), "组地址")
    except ValueError as exc:
        return jsonify(ok=False, code="invalid_input",
                       error={"message": str(exc)}), 400
    rev_id = body.get("revision_id")
    result = service().trial_send(
        source, group, revision_id=int(rev_id) if rev_id is not None else None)
    if not result.get("ok"):
        return jsonify(result), 409
    return jsonify(result)


@bp.post("/api/evaluate")
def api_evaluate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, code="invalid_input",
                       error={"message": "请求体必须是 JSON 对象"}), 400
    try:
        result = service().evaluate_payload(data)
    except ValidationError as exc:
        return jsonify(ok=False, code="invalid_input",
                       error=exc.to_dict()), 400
    return jsonify(result)


@bp.get("/api/revisions")
def api_revisions():
    return jsonify(service().storage.list_revisions(limit=100))


# ======================================================================
# helpers
# ======================================================================

def _resolve_revision(svc, rev_id):
    if rev_id is None:
        rev = svc.active_revision()
    else:
        rev = svc.storage.get_revision(rev_id)
        if rev is not None and rev["status"] != "published":
            rev = None
    snapshot = rev["payload"] if rev else None
    return rev, snapshot


def _published_revisions(svc):
    return [r for r in svc.storage.list_revisions()
            if r["status"] == "published"]


def _default_form_model() -> dict:
    ex = EXAMPLES["three_ports"]
    return {
        "listening": [
            {"source": e["source"] or "", "group": e["group"],
             "ports": ", ".join(e["ports"])}
            for e in ex["listening"]
        ] + [{"source": "", "group": "", "ports": ""} for _ in range(2)],
        "traffic": [
            {"source": f["source"], "group": f["group"],
             "frames": f["frames"]}
            for f in ex["traffic"]
        ] + [{"source": "", "group": "", "frames": ""} for _ in range(2)],
        "hw_limit": ex["limits"]["hw"],
        "cpu_limit": ex["limits"]["cpu"],
        "simulate_fail": False,
    }


def _form_model_from_form(form) -> dict:
    l_src = form.getlist("l_source")
    l_grp = form.getlist("l_group")
    l_prt = form.getlist("l_ports")
    listening = [
        {"source": l_src[i] if i < len(l_src) else "",
         "group": l_grp[i] if i < len(l_grp) else "",
         "ports": l_prt[i] if i < len(l_prt) else ""}
        for i in range(max(len(l_src), len(l_grp), len(l_prt)))
    ]
    t_src = form.getlist("t_source")
    t_grp = form.getlist("t_group")
    t_frm = form.getlist("t_frames")
    traffic = [
        {"source": t_src[i] if i < len(t_src) else "",
         "group": t_grp[i] if i < len(t_grp) else "",
         "frames": t_frm[i] if i < len(t_frm) else ""}
        for i in range(max(len(t_src), len(t_grp), len(t_frm)))
    ]
    return {
        "listening": listening,
        "traffic": traffic,
        "hw_limit": form.get("hw_limit", ""),
        "cpu_limit": form.get("cpu_limit", ""),
        "simulate_fail": form.get("simulate_fail") in ("1", "on", "true", "yes"),
    }


def _drill_payload_from_form(form) -> dict:
    model = _form_model_from_form(form)
    return {
        "listening": [
            {"source": r["source"], "group": r["group"], "ports": r["ports"]}
            for r in model["listening"]
        ],
        "traffic": [
            {"source": r["source"], "group": r["group"], "frames": r["frames"]}
            for r in model["traffic"]
        ],
        "limits": {"hw": model["hw_limit"], "cpu": model["cpu_limit"]},
        "simulate_fail": model["simulate_fail"],
    }


def _issue_map(issues: list[dict]) -> dict:
    out: dict[str, list[str]] = {}
    for iss in issues:
        scope = iss.get("scope", "")
        row = iss.get("row")
        field = iss.get("field", "")
        key = f"{scope}:{row}:{field}" if row else f"{scope}:{field}"
        out.setdefault(key, []).append(iss["message"])
        out.setdefault("_all", []).append(
            f"[{scope}" + (f" 第{row}行" if row else "") + "] " + iss["message"])
    return out
