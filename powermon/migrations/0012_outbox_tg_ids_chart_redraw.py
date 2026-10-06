# Hand-written, quick task 261006-qv7 (DATA-02 amended: a removal deletes the outage's
# alerts and redraws today's chart).
#
# Expand-only, for rollback safety: every new column is nullable with no default, no
# db_default and no backfill, so PostgreSQL adds them without a table rewrite, and the
# previous release (a7e984c), which inserts outbox and chart rows without these columns,
# keeps working on this schema (they stay NULL). NULL means "no message id stored" (every
# alert sent before this release), "no delete requested" and "no redraw requested", which
# is true of every existing row. The CHECK only constrains rows that carry a delete
# request, which old code never writes, and the partial index covers only those rows.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('powermon', '0011_location_chart_refresh_min'),
    ]

    operations = [
        migrations.AddField(
            model_name='outboxmessage',
            name='tg_chat_id',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='tg_message_id',
            field=models.BigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='delete_requested_at',
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name='outboxmessage',
            name='delete_result',
            field=models.CharField(max_length=32, null=True),
        ),
        migrations.AddConstraint(
            model_name='outboxmessage',
            constraint=models.CheckConstraint(
                condition=models.Q(('delete_requested_at__isnull', True))
                | models.Q(
                    ('status', 'sent'),
                    ('tg_chat_id__isnull', False),
                    ('tg_message_id__isnull', False),
                ),
                name='outbox_delete_needs_ids',
            ),
        ),
        migrations.AddIndex(
            model_name='outboxmessage',
            index=models.Index(
                condition=models.Q(
                    ('delete_requested_at__isnull', False), ('delete_result__isnull', True)
                ),
                fields=['location', 'id'],
                name='outbox_delete_due_idx',
            ),
        ),
        migrations.AddField(
            model_name='chartmessage',
            name='redraw_requested_at',
            field=models.DateTimeField(null=True),
        ),
    ]
