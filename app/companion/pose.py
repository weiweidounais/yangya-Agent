"""姿态检查适配层（尚未接入）。

实时姿态必须在端侧完成（MediaPipe / TensorFlow.js / 原生），视频帧不出设备。
服务端这里只提供接口与状态，不做假分析：

* ``pose_status()`` 告诉前端/模型当前是否可用；
* ``analyze_pose()`` 在未接入时返回 ``not_connected``，不给任何"看起来对/错"的结论。

接入时的约束：帧率 ≤ 5 fps、只回结构化关键点与规则判定、不存视频、不上传原始画面。
"""

from __future__ import annotations

from typing import Any

CAPABILITIES = {
    "on_device_required": True,
    "stores_video": False,
    "uploads_frames": False,
    "checks": ["深蹲膝轨", "硬拉背线", "俯卧撑肘角", "跑步落地"],
}


def pose_status() -> dict[str, Any]:
    return {
        "configured": False,
        "status": "not_connected",
        "message": "姿态检查需要在手机端开启摄像头本地分析；当前版本没有接入。",
        "capabilities": CAPABILITIES,
    }


def analyze_pose(frame: bytes) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "not_connected",
        "message": "这一版还不能看动作。组间休息时可以先文字告诉我动作和感受，我给调整建议。",
    }
