# Hand-written for quick task 261006-of9 (CHRT-02 amended): the per-location chart update
# period, in minutes.
#
# Expand-only. db_default keeps DEFAULT 15 on the column, so a rollback to 6f248ff (code
# that does not know the column) can still add a location (README section 8). Existing
# rows read 15 (a PostgreSQL fast default: no table rewrite).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('powermon', '0010_chart_message_history_reset_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='location',
            name='chart_refresh_min',
            field=models.IntegerField(
                choices=[
                    (1, '1 min'),
                    (5, '5 min'),
                    (10, '10 min'),
                    (15, '15 min'),
                    (30, '30 min'),
                    (60, '1 hour'),
                ],
                db_default=15,
                default=15,
            ),
        ),
        migrations.AddConstraint(
            model_name='location',
            constraint=models.CheckConstraint(
                condition=models.Q(('chart_refresh_min__in', (1, 5, 10, 15, 30, 60))),
                name='location_chart_refresh_valid',
            ),
        ),
    ]
