"""
MinIO URL 真实测试
通过 /api/resume/async/batch/multiform 的 type=url 模式，
从本地 MinIO (localhost:9000) 下载 4 份简历并解析打标。
"""
import json, sys, time, urllib.parse
from pathlib import Path
import requests

HOST      = "http://localhost:21804"
MINIO     = "http://localhost:9000/resumes"
BATCH_EP  = f"{HOST}/api/resume/async/batch/multiform"
RESULT_EP = f"{HOST}/api/resume/async/result"

STATUS_DESC = {0:"待处理", 1:"简历解析中", 2:"标签打标中", 3:"任务完成", 4:"任务失败"}
SEP  = "─" * 70
SEP2 = "═" * 70

RESUMES = [
    "31岁-5年经验-证券事务代表-猎聘简历.pdf",
    "PKU刘旗简历.pdf",
    "e0b43fc5-7d60-4922-b223-e3c1b32a298e.pdf",
    "伍先生-34岁-11年经验-证券事务代表-猎聘简历.pdf",
]


def minio_url(fname: str) -> str:
    """生成包含安全 URL 编码文件名的 MinIO 地址。

    Args:
        fname: MinIO 对象的中文或英文文件名。

    Returns:
        可供 HTTP 客户端访问的对象 URL。
    """
    return f"{MINIO}/{urllib.parse.quote(fname)}"


def submit():
    """以 URL 模式批量提交预设的 MinIO 简历。

    Returns:
        文件名与任务标识组成的列表；提交失败项的任务标识为 ``None``。

    Raises:
        requests.HTTPError: 批量提交接口返回非成功 HTTP 状态。
    """
    resumes_payload = [
        {"type": "url", "file": {"name": fname, "path": minio_url(fname)}}
        for fname in RESUMES
    ]

    print(f"\n{SEP2}")
    print("  提交 4 份简历（全部 type=url，从 MinIO 下载）")
    print(SEP2)
    for r in resumes_payload:
        print(f"  → {r['file']['name']}")
        print(f"    {r['file']['path']}")

    resp = requests.post(
        BATCH_EP,
        data={
            "resumes":        json.dumps(resumes_payload, ensure_ascii=False),
            "jobTitle":       "综合测试岗",
            "requiredSkills": "测试,验证",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()

    print(f"\n  HTTP {resp.status_code}  code={body['code']}")
    print(f"  total={body['total']}  submitted={body['submitted']}  failed={body['failed']}")

    task_ids = []
    for t in body.get("tasks", []):
        if t["code"] == 0:
            print(f"  ✓ {t['filename']}  taskId={t['taskId']}")
            task_ids.append((t["filename"], t["taskId"]))
        else:
            print(f"  ✗ {t['filename']}  错误: {t.get('msg')}")
            task_ids.append((t["filename"], None))
    return task_ids


def poll(fname: str, task_id: str, timeout: int = 300):
    """轮询 MinIO 简历任务直至完成、失败或超时。

    Args:
        fname: 用于输出进度的文件名。
        task_id: 服务端任务标识。
        timeout: 最大轮询时长，单位为秒。

    Returns:
        成功时返回结果字典，业务失败或超时时返回 ``None``。
    """
    start, last = time.time(), -1
    while time.time() - start < timeout:
        body = requests.get(RESULT_EP, params={"taskId": task_id}, timeout=10).json()
        if body["code"] != 0:
            print(f"  [{fname}] 查询异常: {body}")
            return None
        data    = body["data"]
        status  = data["status"]
        elapsed = data.get("elapsedSeconds", 0)
        if status != last:
            print(f"  [{fname}] [{STATUS_DESC.get(status, status)}] {elapsed:.0f}s")
            last = status
        if status == 3:
            return data["result"]
        if status == 4:
            print(f"  [{fname}] 失败: {data.get('errorMsg')}")
            return None
        time.sleep(3 if elapsed < 15 else (5 if elapsed < 60 else 10))
    print(f"  [{fname}] 超时")
    return None


def print_result(fname: str, r: dict):
    """向控制台输出解析和打标结果摘要。

    Args:
        fname: 当前简历文件名。
        r: 完成任务返回的结果字典。
    """
    c  = r.get("candidate", {})
    gm = {1:"男", 2:"女"}
    print(f"\n{SEP}")
    print(f"  文件: {fname}")
    print(f"  姓名: {c.get('name','')}  性别: {gm.get(c.get('gender'),'?')}  年龄: {c.get('age') or '-'}")
    print(f"  电话: {c.get('phone','')}  邮箱: {c.get('email','')}")
    works = r.get("works", [])
    print(f"  工作经历({len(works)}): " + " | ".join(
        f"{w.get('company','')}[{w.get('position','')}]" for w in works[:3]))
    for tag in r.get("positiveTags", {}).get("tagsArray", []):
        vals = [v["tag"] for v in tag.get("value", [])]
        if vals:
            print(f"  [{tag.get('displayName','')}] {', '.join(vals)}")
    neg = r.get("negativeTags", {})
    hits = sum(len(g.get("value", [])) for g in neg.get("tagsArray", []))
    print(f"  风险标签: {hits} 个  扣分: {neg.get('deductionScore', 0)}")
    print(f"  解析: {r.get('resumeParseFinishTime','')}  打标: {r.get('tagParseFinishTime','')}")


def main():
    """检查服务及 MinIO 连通性并执行预设批量验证。"""
    # 健康检查
    try:
        requests.get(f"{HOST}/health", timeout=5).raise_for_status()
        print(f"✓ 解析服务在线: {HOST}")
    except Exception as e:
        print(f"✗ 服务不可达: {e}"); sys.exit(1)

    # 验证 MinIO URL 可达
    print(f"\n  验证 MinIO URL 可访问性...")
    for fname in RESUMES:
        url = minio_url(fname)
        r = requests.head(url, timeout=5)
        ct = r.headers.get("Content-Type", "")
        print(f"  HTTP {r.status_code}  {ct:30s}  {fname}")

    t0 = time.time()
    task_ids = submit()

    print(f"\n{SEP}")
    print("  开始轮询...")
    print(SEP)

    ok = fail = 0
    for fname, tid in task_ids:
        if tid is None:
            fail += 1
            continue
        result = poll(fname, tid)
        if result:
            print_result(fname, result)
            ok += 1
        else:
            fail += 1

    print(f"\n{SEP2}")
    print(f"  完成  成功 {ok} / 失败 {fail} / 共 {len(task_ids)}  总耗时 {time.time()-t0:.1f}s")
    print(SEP2)


if __name__ == "__main__":
    main()
