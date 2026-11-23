"""Compatibility marker for the previously deployed integration candidate.

Mock storage is initialized explicitly by the local integration environment.
The candidate may already have created the table. Leave its evidence intact.
"""

revision = "coarse_sorting_mock_round"
down_revision = "sorting_task_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
