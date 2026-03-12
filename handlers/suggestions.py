"""Handler for user-suggested posts (Предложка)."""

import logging
from core.group_context import GroupContext
from database.service import create_suggested_post

logger = logging.getLogger(__name__)


async def handle_suggestion(ctx: GroupContext, from_id: int, text: str, peer_id: int) -> str | None:
    """
    Handle !предложить command — user suggests a post for the group wall.
    Returns reply text.
    """
    content = text.strip()
    if not content:
        return "Формат: !предложить <текст поста>\n\nНапиши текст, который хочешь предложить для публикации."

    post = await create_suggested_post(
        group_id=ctx.group_id,
        from_vk_id=from_id,
        text=content,
    )

    # Notify admin
    try:
        await ctx.api.messages.send(
            user_id=ctx.admin_vk_id,
            message=(
                f"Новое предложение для стены (#{post.id}):\n\n"
                f"От: vk.com/id{from_id}\n"
                f"Текст: {content[:500]}\n\n"
                f"Ответьте:\n"
                f"/принять {post.id} — опубликовать\n"
                f"/отклонить {post.id} причина — отклонить"
            ),
            random_id=0,
        )
    except Exception as e:
        logger.warning(f"Failed to notify admin about suggestion #{post.id}: {e}")

    return f"Спасибо! Ваше предложение #{post.id} отправлено на рассмотрение администратору."
