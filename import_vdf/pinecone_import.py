import pandas as pd
from tqdm import tqdm
from export_vdf.util import standardize_metric_reverse
from import_vdf.vdf_import_cls import ImportVDF
from pinecone import Pinecone, ServerlessSpec, PodSpec, Vector
import os
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 1000  # Set the desired batch size


class ImportPinecone(ImportVDF):
    def __init__(self, args):
        self.db_name = "pinecone"
        super().__init__(args)
        self.pc = Pinecone(api_key=self.args["pinecone_api_key"])

    def upsert_data(self):
        # Iterate over the indexes and import the data
        for index_name, index_meta in tqdm(
            self.vdf_meta["indexes"].items(), desc="Importing indexes"
        ):
            print(f"Importing data for index '{index_name}'")
            # list indexes
            indexes = self.pc.list_indexes().names()
            # check if index exists
            suffix = 2
            while index_name in indexes and self.args["create_new"] is True:
                index_name = index_name + f"-{suffix}"
                suffix += 1
            if index_name not in indexes:
                # create index
                try:
                    if self.args["serverless"] is True:
                        self.pc.create_index(
                            name=index_name,
                            dimension=index_meta[0]["dimensions"],
                            metric=standardize_metric_reverse(
                                index_meta[0]["metric"], "pinecone"
                            ),
                            spec=ServerlessSpec(
                                cloud=self.args["cloud"],
                                region=self.args["region"],
                            ),
                        )
                    else:
                        self.pc.create_index(
                            name=index_name,
                            dimension=index_meta[0]["dimensions"],
                            metric=standardize_metric_reverse(
                                index_meta[0]["metric"], "pinecone"
                            ),
                            spec=PodSpec(
                                environment=self.args["environment"],
                                pod_type=self.args["pod_type"]
                                if (
                                    "pod_type" in self.args
                                    and self.args["pod_type"] is not None
                                )
                                else "starter",
                            ),
                        )
                except Exception as e:
                    print(e)
                    raise Exception(f"Invalid index name '{index_name}'", e)
            index = self.pc.Index(index_name)
            current_batch_size = BATCH_SIZE
            for namespace_meta in tqdm(index_meta, desc="Importing namespaces"):
                print(f"Importing data for namespace '{namespace_meta['namespace']}'")
                namespace = namespace_meta["namespace"]
                data_path = namespace_meta["data_path"]

                # Check if the data path exists
                final_data_path = os.path.join(
                    self.args["cwd"], self.args["dir"], data_path
                )
                if not os.path.isdir(final_data_path):
                    raise Exception(
                        f"Invalid data path for index '{index_name},\n"
                        f"data_path: {data_path}',\n"
                        f"Joined path: {final_data_path}'"
                        f"Current working directory: {self.args['cwd']}'\n"
                        f"Command line arg (dir): {self.args['dir']}'"
                    )

                # Load the data from the parquet files
                parquet_files = sorted(
                    [
                        file
                        for file in os.listdir(final_data_path)
                        if file.endswith(".parquet")
                    ]
                )

                vectors = {}
                metadata = {}
                vector_column_names, vector_column_name = self.get_vector_column_name(
                    index_name, namespace_meta
                )

                for file in tqdm(parquet_files, desc="Loading data from parquet files"):
                    file_path = os.path.join(final_data_path, file)
                    df = pd.read_parquet(file_path)
                    vectors.update(
                        {
                            row["id"]: row[vector_column_name].tolist()
                            for _, row in df.iterrows()
                        }
                    )
                    metadata.update(
                        {
                            row["id"]: {
                                key: value
                                for key, value in row.items()
                                if key not in ["id"] + vector_column_names
                            }
                            for _, row in df.iterrows()
                        }
                    )
                print(
                    f"Loaded {len(vectors)} vectors from {len(parquet_files)} parquet files"
                )
                # Upsert the vectors and metadata to the Pinecone index in batches
                imported_count = 0
                start_idx = 0
                while start_idx < len(vectors):
                    end_idx = min(start_idx + current_batch_size, len(vectors))

                    batch_vectors = [
                        Vector(
                            id=str(id),
                            values=vector,
                            metadata={
                                k: v
                                for k, v in metadata.get(id, {}).items()
                                if v is not None
                            },
                        )
                        if len(metadata.get(id, {}).keys()) > 0
                        else Vector(
                            id=str(id),
                            values=vector,
                        )
                        for id, vector in list(vectors.items())[start_idx:end_idx]
                    ]
                    try:
                        resp = index.upsert(vectors=batch_vectors, namespace=namespace)
                        imported_count += resp["upserted_count"]
                        start_idx += resp["upserted_count"]
                    except Exception as e:
                        print(f"Error upserting vectors for index '{index_name}'", e)
                        if current_batch_size < BATCH_SIZE / 100:
                            print("Batch size is not the issue. Aborting import")
                            raise e
                        current_batch_size = int(9 * current_batch_size / 10)
                        print(f"Reducing batch size to {current_batch_size}")
                        continue
        print(f"Data import completed successfully. Imported {imported_count} vectors")
