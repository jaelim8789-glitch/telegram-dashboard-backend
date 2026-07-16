from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_ops_report import AiOpsReport


async def create_report(db: AsyncSession, *, report: str, anomalies_json: str) -> AiOpsReport:
    row = AiOpsReport(report=report, anomalies_json=anomalies_json)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_recent_reports(db: AsyncSession, *, limit: int = 20) -> list[AiOpsReport]:
    result = await db.execute(select(AiOpsReport).order_by(AiOpsReport.created_at.desc()).limit(limit))
    return list(result.scalars().all())
