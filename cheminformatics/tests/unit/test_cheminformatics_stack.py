import os

import aws_cdk as core
import aws_cdk.assertions as assertions

from cheminformatics.cheminformatics_stack import CheminformaticsStack

# example tests. To run these tests, uncomment this file along with the example
# resource in cheminformatics/cheminformatics_stack.py
def test_cheminformatics_stack_cluster_created():
    app = core.App(
        context={
            "create_new_vpc": "True",
            "existing_vpc_name": "SomeVpcName",
            "cidr_block": "10.0.0.0/24",
            "number_of_azs": 2,
            "ec2_volume_size": 100,
            "instance_type": "p3.2xlarge",
            "cheminformatics_container": "public.ecr.aws/b9g4r0v3/cheminformatics_demo:0.1.2",
            "megamolbart_container": "nvcr.io/nvidia/clara/megamolbart:0.1.2",
            "megamolbart_model_url": "https://api.ngc.nvidia.com/v2/models/nvidia/clara/megamolbart/versions/0.1/zip",
        }
    )
    stack = CheminformaticsStack(
        app,
        "cheminformatics",
        env={
            "region": os.getenv("CDK_DEFAULT_REGION"),
            "account": os.getenv("CDK_DEFAULT_ACCOUNT"),
        },
    )
    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::ECS::Cluster",
        {
            "ClusterSettings": [
                {"Name": "containerInsights", "Value": "enabled"}
            ]
        },
    )
