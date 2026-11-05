"""Identity bootstrap used only by verified channel ingress/delivery."""
from app.data_access import system_scope


def resolve_channel_identity(channel, external_user_id):
    from app.channels.binding import resolve_user_id
    with system_scope():
        return resolve_user_id(channel, external_user_id)


def rebind_channel_identity(channel, external_user_id, user_id, *, display_name=""):
    """Trusted channel-boundary upsert of the globally unique external identity.

    Ordinary user tools must use the scoped binding read/list/delete functions.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.channels.binding import ChannelBinding
    from app.data_access.session import SessionLocal
    with system_scope(), SessionLocal() as session:
        row = session.execute(
            select(ChannelBinding).where(
                ChannelBinding.channel == channel,
                ChannelBinding.external_user_id == external_user_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = ChannelBinding(
                channel=channel,
                external_user_id=external_user_id,
                user_id=user_id,
                display_name=display_name or "",
            )
            session.add(row)
        else:
            row.user_id = user_id
            row.display_name = display_name or row.display_name
            row.updated_at = datetime.now(timezone.utc)
        session.commit()
        return {
            "channel": row.channel,
            "external_user_id": row.external_user_id,
            "user_id": row.user_id,
            "display_name": row.display_name,
        }
