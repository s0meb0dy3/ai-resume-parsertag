"""
异步接口并发压测：同时提交 N 份简历，并行轮询，汇总耗时与结果。
用法：
  python stress_test_async.py                         # 测试 uploads/test_full 全部文件（默认并发）
  python stress_test_async.py --concurrency 4         # 同时提交 4 份（默认全部文件数量）
  python stress_test_async.py --repeat 2              # 每个文件提交 2 次（模拟队列积压）
"""
import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import aiohttp

HOST       = "http://localhost:21804"
_BASE      = Path(__file__).parent.parent   # 项目根目录
TEST_DIR   = _BASE / "uploads"
RESULT_DIR = _BASE / "results" / "stress_test"
LOG_DIR    = _BASE / "logs"

STATUS_DESC = {0: "待处理", 1: "解析中", 2: "打标中", 3: "完成", 4: "失败"}


async def submit_one(session: aiohttp.ClientSession, file_path: Path, idx: int) -> str:
    """向异步解析接口提交一份 PDF。

    Args:
        session: 复用连接池的 HTTP 会话。
        file_path: 待提交的 PDF 路径。
        idx: 用于压测日志定位的任务序号。

    Returns:
        服务端生成的任务标识。

    Raises:
        RuntimeError: 业务响应码表示提交失败。
    """
    with open(file_path, "rb") as f:
        data = aiohttp.FormData()
        data.add_field("file", f, filename=file_path.name, content_type="application/pdf")
        data.add_field("jobTitle", "大模型Agent算法工程师")
        async with session.post(f"{HOST}/api/resume/async/parse", data=data) as resp:
            body = await resp.json(content_type=None)
    if body["code"] != 0:
        raise RuntimeError(f"[{idx}] 提交失败: {body}")
    task_id = body["data"]["taskId"]
    print(f"  [{idx:02d}] 提交成功 taskId={task_id[:8]}… file={file_path.name[:30]}")
    return task_id


async def poll_one(session: aiohttp.ClientSession, task_id: str, idx: int,
                   submit_ts: float, timeout: int = 300) -> dict:
    """轮询单个异步任务直至终态或超时。

    Args:
        session: 复用连接池的 HTTP 会话。
        task_id: 服务端任务标识。
        idx: 用于压测日志定位的任务序号。
        submit_ts: 任务提交前的 Unix 时间戳。
        timeout: 最大轮询时长，单位为秒。

    Returns:
        包含成功标志、耗时以及结果或错误信息的汇总字典。
    """
    start      = time.time()
    last_status = -1
    while time.time() - start < timeout:
        async with session.get(
            f"{HOST}/api/resume/async/result", params={"taskId": task_id}
        ) as resp:
            body = await resp.json(content_type=None)
        data   = body["data"]
        status = data["status"]
        if status != last_status:
            elapsed_wall = time.time() - submit_ts
            print(f"  [{idx:02d}] {STATUS_DESC.get(status, status)} "
                  f"(任务={data.get('elapsedSeconds', 0):.0f}s / 墙钟={elapsed_wall:.0f}s)")
            last_status = status
        if status == 3:
            return {"ok": True, "task_id": task_id,
                    "elapsed": time.time() - submit_ts, "result": data["result"]}
        if status == 4:
            return {"ok": False, "task_id": task_id,
                    "elapsed": time.time() - submit_ts, "error": data.get("errorMsg")}
        interval = 2 if data.get("elapsedSeconds", 0) < 15 else 4
        await asyncio.sleep(interval)
    return {"ok": False, "task_id": task_id,
            "elapsed": time.time() - submit_ts, "error": "timeout"}


async def run_one(session: aiohttp.ClientSession, file_path: Path,
                  idx: int, submit_ts: float) -> dict:
    """执行一份简历的提交和轮询，并把异常转为失败结果。

    Args:
        session: 复用连接池的 HTTP 会话。
        file_path: 待提交的 PDF 路径。
        idx: 用于压测日志定位的任务序号。
        submit_ts: 整批压测开始时的 Unix 时间戳。

    Returns:
        可直接参与批量统计的任务结果字典。
    """
    try:
        task_id = await submit_one(session, file_path, idx)
        return await poll_one(session, task_id, idx, submit_ts)
    except Exception as e:
        return {"ok": False, "task_id": "", "elapsed": time.time() - submit_ts, "error": str(e)}


async def main_async(files: list[Path], concurrency: int):
    """并发执行压测并将统计结果写入本地文件。

    Args:
        files: 按提交顺序排列的 PDF 路径列表。
        concurrency: 同时运行的任务上限。

    Note:
        本函数会创建结果和日志目录，并写入 JSON 汇总及文本日志。
    """
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    ts_start = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"stress_{ts_start}.log"
    _log_lines: list[str] = []

    def tee(msg: str):
        """同时向控制台和内存日志缓冲区写入消息。

        Args:
            msg: 待记录的文本。
        """
        print(msg)
        _log_lines.append(msg)

    def flush_log():
        """将内存日志缓冲区一次性写入压测日志文件。"""
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(_log_lines) + "\n")

    total    = len(files)
    sem      = asyncio.Semaphore(concurrency)
    wall_t0  = time.time()

    tee(f"\n{'='*70}")
    tee(f"并发压测  文件数={total}  并发上限={concurrency}")
    tee(f"服务地址: {HOST}")
    tee(f"{'='*70}\n")

    async def bounded(fp: Path, idx: int):
        """在并发信号量保护下运行单份简历任务。

        Args:
            fp: 待提交的 PDF 路径。
            idx: 用于压测日志定位的任务序号。

        Returns:
            单份简历的任务结果字典。
        """
        async with sem:
            return await run_one(session, fp, idx, wall_t0)

    connector = aiohttp.TCPConnector(limit=concurrency + 4)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks  = [bounded(fp, i + 1) for i, fp in enumerate(files)]
        results = await asyncio.gather(*tasks)

    wall_elapsed = time.time() - wall_t0
    ok    = [r for r in results if r["ok"]]
    fail  = [r for r in results if not r["ok"]]
    times = [r["elapsed"] for r in ok]

    tee(f"\n{'='*70}")
    tee(f"压测结果汇总")
    tee(f"{'─'*70}")
    tee(f"  总任务数 : {total}")
    tee(f"  成功     : {len(ok)}")
    tee(f"  失败     : {len(fail)}")
    tee(f"  墙钟总耗时: {wall_elapsed:.1f}s")
    if times:
        tee(f"  单任务最短: {min(times):.1f}s")
        tee(f"  单任务最长: {max(times):.1f}s")
        tee(f"  单任务均值: {sum(times)/len(times):.1f}s")
    if fail:
        tee(f"\n  失败详情:")
        for r in fail:
            tee(f"    taskId={r['task_id'][:8]}... err={r['error']}")

    # 保存汇总 JSON
    ts_end  = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {
        "timestamp": ts_end,
        "concurrency": concurrency,
        "total": total,
        "success": len(ok),
        "fail": len(fail),
        "wallElapsed": round(wall_elapsed, 2),
        "minTaskElapsed": round(min(times), 2) if times else None,
        "maxTaskElapsed": round(max(times), 2) if times else None,
        "avgTaskElapsed": round(sum(times) / len(times), 2) if times else None,
        "details": [
            {"file": files[i].name, "ok": r["ok"],
             "elapsed": round(r["elapsed"], 2),
             "error": r.get("error")}
            for i, r in enumerate(results)
        ],
    }
    out = RESULT_DIR / f"stress_summary_{ts_end}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    tee(f"\n  汇总已保存: results/stress_test/{out.name}")

    # 保存运行日志到 logs/
    flush_log()
    tee(f"  运行日志已保存: logs/{log_path.name}")
    tee(f"{'='*70}\n")


def main():
    """解析命令行参数并启动异步并发压测。"""
    parser = argparse.ArgumentParser(description="异步接口并发压测")
    parser.add_argument("--dir",         default=str(TEST_DIR), help="测试目录")
    parser.add_argument("--concurrency", type=int, default=0,
                        help="并发数（默认=文件数，即全并发）")
    parser.add_argument("--repeat",      type=int, default=1,
                        help="每个文件重复提交次数（模拟队列积压）")
    args = parser.parse_args()

    test_dir = Path(args.dir)
    pdfs     = sorted(test_dir.glob("*.pdf"))
    if not pdfs:
        print(f"✗ 未找到 PDF: {test_dir}")
        return

    files  = pdfs * args.repeat
    concur = args.concurrency if args.concurrency > 0 else len(files)
    asyncio.run(main_async(files, concur))


if __name__ == "__main__":
    main()
