"""An explicit store measurement run never shares or evicts the dispatch ingress queue."""

import yaml
from tests.workflow_files import adopter_view


def test_explicit_measurement_cannot_be_evicted_by_unrelated_ingress():
    workflow = yaml.safe_load(adopter_view("fixed-model-factory-dispatch.yml"))
    group = workflow["concurrency"]["group"]
    # A measurement run gets its own run-unique group; targeted work its own
    # target/action group. Neither shares the ingress queue.
    assert "inputs.measure && format('factory-measure-{0}', github.run_id)" in group
    assert "inputs.target && format('factory-target-{0}-{1}', inputs.target" in group
    assert group.endswith("|| 'fixed-model-factory-dispatch-ingress' }}")
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["dispatch"]["concurrency"]["group"] == "fixed-model-factory-dispatch"
