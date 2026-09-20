"""黄河影像征集与融合报道后端。

- 断点续传按内容哈希去重，不重复生成作品；
- 原件与转码件按内容哈希组织；
- 技术检查输出可复核报告（实测值 / 阈值 / 规则版本）；
- 相似、疑似搬运、疑似合成只产生人工核验任务，不直接定性；
- 授权关联提交时条款版本，补传说明、替换文件、权属争议、撤稿各自形成事件；
- 选用、复核、发布职责分离，渠道回执幂等汇总；
- 公开查询不暴露联系方式与未采用素材。
"""

from .core import RiverflowService, load_policy
from .storage import BlobStore

__all__ = ["RiverflowService", "BlobStore", "load_policy"]
