"""
A CDK Module that represents a reproducable cheminformatics-megamolbart deployment
"""

from aws_cdk import (
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_iam as iam,
    aws_cloudwatch as cloudwatch,
    aws_autoscaling as autoscaling,
    aws_ecs_patterns as ecs_patterns,
    aws_logs as logs,
    Stack,
    RemovalPolicy,
    Duration,
)
from constructs import Construct


class MegamolbartStack(Stack):
    """
    Megamolbart stack which deploys all the necessary resources
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        self.identifier = "Megamolbart"
        self.volume_name = "megamolbart-data-volume"

        self._create_vpc()
        self._create_ecs_cluster()
        self._create_gpu_capacity()
        self._create_efs_volume()
        self._create_megamolbart_service()

    def _create_vpc(self):

        # Create a new vpc with 2 availability zones
        if self.node.try_get_context("create_new_vpc") == "True":
            self.vpc = ec2.Vpc(
                self,
                f"{self.identifier}-VPC",
                cidr=self.node.try_get_context("cidr_block"),
                max_azs=self.node.try_get_context("number_of_azs"),
            )
        else:
            # Use existing vpc
            self.vpc = ec2.Vpc.from_lookup(
                self,
                "VPC",
                vpc_name=self.node.try_get_context("existing_vpc_name"),
            )

    def _create_ecs_cluster(self):
        cluster = ecs.Cluster(
            self,
            f"{self.identifier}-Cluster",
            vpc=self.vpc,
            container_insights=True,
        )

        self.cluster = cluster

    def _create_gpu_capacity(self):
        commands_user_data = ec2.UserData.for_linux()

        # Make the default runtime nvidia so that multiple tasks
        # can share 1 gpu
        commands_user_data.add_commands(
            """
            sed -i 's/^OPTIONS="/OPTIONS="--default-runtime nvidia /' /etc/sysconfig/docker && systemctl restart docker && \
            yum install python2-pip -y && \
            pip install nvidia-ml-py boto3 && \
            curl https://s3.amazonaws.com/aws-bigdata-blog/artifacts/GPUMonitoring/gpumon.py > gpumon.py && \
            python gpumon.py & 
            """
        )

        auto_scaling_group = autoscaling.AutoScalingGroup(
            self,
            f"{self.identifier}-GPU-ASG",
            vpc=self.vpc,
            instance_type=ec2.InstanceType(
                self.node.try_get_context("instance_type")
            ),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2(
                hardware_type=ecs.AmiHardwareType.GPU
            ),
            user_data=commands_user_data,
            min_capacity=1,
            max_capacity=2,
            block_devices=[
                autoscaling.BlockDevice(
                    device_name="/dev/xvda",
                    volume=autoscaling.BlockDeviceVolume.ebs(
                        self.node.try_get_context("ec2_volume_size")
                    ),
                ),
            ],
        )
        autoscaling.StepScalingPolicy(
            self,
            "StepScalingPolicy",
            auto_scaling_group=auto_scaling_group,
            metric=self.cluster.metric_cpu_utilization(),
            scaling_steps=[
                autoscaling.ScalingInterval(upper=15, change=-1), 
                autoscaling.ScalingInterval(lower=30, change=+1), 
                autoscaling.ScalingInterval(lower=50, change=+1)
            ],
            adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY     
        )

        auto_scaling_group.role.attach_inline_policy(
            iam.Policy(
                self,
                "cloudwatch-put",
                statements=[
                    iam.PolicyStatement(
                        actions=["cloudwatch:PutMetricData"],
                        resources=["*"]
                    )
                ]
            )
        )

        capacity_provider = ecs.AsgCapacityProvider(
            self,
            f"{self.identifier}-GPU-AsgCapacityProvider",
            auto_scaling_group=auto_scaling_group,
            enable_managed_termination_protection=False,
            enable_managed_scaling=False
        )

        self.cluster.add_asg_capacity_provider(capacity_provider)
    
    def _create_efs_volume(self):

        efs_filesystem = efs.FileSystem(
            self,
            f"{self.identifier}-EFSFilesystem",
            vpc=self.vpc,
            removal_policy=RemovalPolicy.DESTROY,
        )

        efs_volume_configuration = ecs.EfsVolumeConfiguration(
            file_system_id=efs_filesystem.file_system_id,
        )
        self.efs_filesystem = efs_filesystem
        self.efs_volume_configuration = efs_volume_configuration

    def _create_megamolbart_service(self):
        task_definition = ecs.Ec2TaskDefinition(
            self,
            "Megamolbart-Task",
            network_mode=ecs.NetworkMode.AWS_VPC,
            placement_constraints=[
                ecs.PlacementConstraint.member_of(
                    f"attribute:ecs.instance-type == {self.node.try_get_context('instance_type')}"
                )
            ],
        )

        task_definition.add_volume(
            name=self.volume_name,
            efs_volume_configuration=self.efs_volume_configuration,
        )

        container = task_definition.add_container(
            "megamolbart",
            image=ecs.ContainerImage.from_registry(
                self.node.try_get_context("megamolbart_container")
            ),
            memory_reservation_mib=4096,
            cpu=2048,
            port_mappings=[ecs.PortMapping(container_port=8888)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=f"{self.identifier}-",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            health_check=ecs.HealthCheck(
                command=[ "CMD-SHELL", "curl -f http://localhost:8888/api || exit 1" ]
            )
        )
        container.add_mount_points(
            ecs.MountPoint(
                container_path="/data",
                read_only=False,
                source_volume=self.volume_name,
            )
        )

        container.add_mount_points(
            ecs.MountPoint(
                container_path="/result",
                read_only=False,
                source_volume=self.volume_name,
            )
        )

        megamolbart = ecs_patterns.ApplicationLoadBalancedEc2Service(
            self,
            f"{self.identifier}-Megamolbart-Service",
            cluster=self.cluster,
            task_definition=task_definition,
            desired_count=3
        )

        megamolbart.target_group.configure_health_check(
            path="/api"
        )
        auto_scaling = megamolbart.service.auto_scale_task_count(
            min_capacity=1, max_capacity=10
        )

        auto_scaling.scale_on_cpu_utilization(
            "CPUScaling",
            target_utilization_percent=30,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )

        self.efs_filesystem.connections.allow_from(
            megamolbart.service, ec2.Port.tcp(2049)
        )
