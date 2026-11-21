from fastapi.responses import Response

from app.evidence import canonical
from app.modules.m7_archive import bundle


def attachment(store, run_id):
    package = bundle.build(store, run_id)
    body = canonical(package)
    return Response(body, media_type='application/json; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename="hall-decay-{run_id[:12]}.json"',
        'X-Evidence-Digest': package['digest'],
        'X-Evidence-Valid': 'true' if package['valid'] else 'false',
        'X-Evidence-Rule-Version': package['rule_version'],
    })
