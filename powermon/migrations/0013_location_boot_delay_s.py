# Hand-written for quick task 261101-g9v (MON-03 amended): the per-location boot delay, in
# seconds, by which the ON transition after an outage is dated earlier.
#
# Expand-only. db_default keeps DEFAULT 0 on the column, so a rollback to 194e808 (code
# that does not know the column) can still add a location (README section 8). Existing
# rows read 0 (a PostgreSQL fast default: no table rewrite), so they behave as before.
# Both operations are reversible; tests/chart/test_bot_key_migration.py migrates back and
# forth across them.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('powermon', '0012_outbox_tg_ids_chart_redraw'),
    ]

    operations = [
        migrations.AddField(
            model_name='location',
            name='boot_delay_s',
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AddConstraint(
            model_name='location',
            constraint=models.CheckConstraint(
                condition=models.Q(('boot_delay_s__gte', 0), ('boot_delay_s__lte', 600)),
                name='location_boot_delay_0_600',
            ),
        ),
    ]
