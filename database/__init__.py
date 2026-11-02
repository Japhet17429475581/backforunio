from database.connection import (
    init_db,
    close_db,
    get_pool,
)

from database.schedule import (
    save_lessons,
)

__all__ = [
    "init_db",
    "close_db",
    "get_pool",
    "save_lessons",
]


from database.users import (
    create_user,
    upgrade_user_to_portal,
    get_user_by_token_hash,
    revoke_account_token,
    set_account_token_hash,
    update_user_profile,
)

__all__ += [
    "create_user",
    "upgrade_user_to_portal",
    "get_user_by_token_hash",
    "revoke_account_token",
    "set_account_token_hash",
    "update_user_profile",
]
