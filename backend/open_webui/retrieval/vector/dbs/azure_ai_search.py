import logging
import json
import time
from typing import Optional, List, Dict, Any
import httpx

from open_webui.retrieval.vector.main import VectorItem, SearchResult, GetResult
from open_webui.config import (
    AZURE_AI_SEARCH_ENDPOINT,
    AZURE_AI_SEARCH_API_KEY,
    AZURE_AI_SEARCH_API_VERSION,
    AZURE_AI_SEARCH_INDEX_PREFIX,
)
from open_webui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["RAG"])


class AzureAISearchClient:
    def __init__(self):
        self.endpoint = AZURE_AI_SEARCH_ENDPOINT.rstrip('/')
        self.api_key = AZURE_AI_SEARCH_API_KEY
        self.api_version = AZURE_AI_SEARCH_API_VERSION
        self.index_prefix = AZURE_AI_SEARCH_INDEX_PREFIX

        # HTTP client for making requests
        self.client = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "api-key": self.api_key
            },
            timeout=30.0
        )

        log.info(f"Azure AI Search client initialized with endpoint: {self.endpoint}")

    def _get_index_name(self, collection_name: str) -> str:
        """Generate index name with prefix"""
        index_name = f"{self.index_prefix}_{collection_name}"
        log.info(f"Azure AI Search: _get_index_name called with collection_name='{collection_name}', generated index_name='{index_name}'")
        return index_name

    def _get_index_url(self, index_name: str) -> str:
        """Get the full URL for index operations"""
        return f"{self.endpoint}/indexes/{index_name}?api-version={self.api_version}"

    def _get_documents_url(self, index_name: str) -> str:
        """Get the full URL for document operations"""
        return f"{self.endpoint}/indexes/{index_name}/docs?api-version={self.api_version}"

    def _get_search_url(self, index_name: str) -> str:
        """Get the full URL for search operations"""
        return f"{self.endpoint}/indexes/{index_name}/docs/search?api-version={self.api_version}"

    def _create_index_schema(self, index_name: str, vector_dimension: int = 1024) -> Dict[str, Any]:
        """Create the index schema for Azure AI Search with dynamic vector dimension"""
        return {
            "name": index_name,
            "fields": [
                {
                    "name": "id",
                    "type": "Edm.String",
                    "key": True,
                    "searchable": False,
                    "filterable": True,
                    "retrievable": True
                },
                {
                    "name": "content",
                    "type": "Edm.String",
                    "searchable": True,
                    "filterable": False,
                    "retrievable": True
                },
                {
                    "name": "contentVector",
                    "type": "Collection(Edm.Single)",
                    "searchable": True,
                    "filterable": False,
                    "retrievable": True,
                    "dimensions": vector_dimension,  # 使用動態維度
                    "vectorSearchProfile": "default-vector-profile"
                },
                {
                    "name": "metadata",
                    "type": "Edm.String",
                    "searchable": False,
                    "filterable": True,
                    "retrievable": True
                }
            ],
            "vectorSearch": {
                "profiles": [
                    {
                        "name": "default-vector-profile",
                        "algorithm": "default-vector-config"
                    }
                ],
                "algorithms": [
                    {
                        "name": "default-vector-config",
                        "kind": "hnsw",
                        "hnswParameters": {
                            "metric": "cosine",
                            "m": 4,
                            "efConstruction": 400,
                            "efSearch": 500
                        }
                    }
                ]
            }
        }

    def _detect_vector_dimension(self, items: List[VectorItem]) -> int:
        """Detect vector dimension from the first item"""
        if not items or not items[0].get("vector"):
            return 1024  # 預設維度
        return len(items[0]["vector"])

    def has_collection(self, collection_name: str) -> bool:
        """Check if the collection (index) exists"""
        try:
            index_name = self._get_index_name(collection_name)
            url = self._get_index_url(index_name)

            response = self.client.get(url)
            return response.status_code == 200
        except Exception as e:
            log.error(f"Error checking collection existence: {e}")
            return False

    def delete_collection(self, collection_name: str):
        """Delete the collection (index)"""
        try:
            index_name = self._get_index_name(collection_name)
            url = self._get_index_url(index_name)

            response = self.client.delete(url)
            if response.status_code in [200, 204, 404]:
                log.info(f"Successfully deleted index: {index_name}")
                return True
            else:
                log.error(f"Failed to delete index: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            log.error(f"Error deleting collection: {e}")
            return False

    def _create_index_if_not_exists(self, collection_name: str, vector_dimension: int = 1024):
        """Create index if it doesn't exist with proper vector dimension"""
        if not self.has_collection(collection_name):
            try:
                index_name = self._get_index_name(collection_name)
                schema = self._create_index_schema(index_name, vector_dimension)

                log.info(f"Creating Azure AI Search index '{index_name}' with vector dimension: {vector_dimension}")

                # Create index
                url = f"{self.endpoint}/indexes?api-version={self.api_version}"
                response = self.client.post(url, json=schema)

                if response.status_code in [200, 201]:
                    log.info(f"Successfully created index: {index_name}")
                    # Wait a bit for index to be ready
                    time.sleep(2)
                else:
                    log.error(f"Failed to create index: {response.status_code} - {response.text}")
                    raise Exception(f"Failed to create index: {response.text}")
            except Exception as e:
                log.error(f"Error creating index: {e}")
                raise
        else:
            # Check if existing index has correct vector dimension
            self._validate_vector_dimension(collection_name, vector_dimension)

    def _validate_vector_dimension(self, collection_name: str, expected_dimension: int):
        """Validate that existing index has correct vector dimension"""
        try:
            index_name = self._get_index_name(collection_name)
            url = self._get_index_url(index_name)

            response = self.client.get(url)
            if response.status_code == 200:
                index_schema = response.json()

                # Find contentVector field
                for field in index_schema.get("fields", []):
                    if field["name"] == "contentVector":
                        current_dimension = field.get("dimensions", 0)
                        if current_dimension != expected_dimension:
                            log.warning(
                                f"Vector dimension mismatch in index '{index_name}': "
                                f"expected {expected_dimension}, found {current_dimension}. "
                                f"You may need to recreate the index."
                            )
                            # Optionally recreate the index
                            self._recreate_index_with_new_dimension(collection_name, expected_dimension)
                        break
        except Exception as e:
            log.error(f"Error validating vector dimension: {e}")

    def _recreate_index_with_new_dimension(self, collection_name: str, vector_dimension: int):
        """Recreate index with new vector dimension"""
        try:
            log.info(f"Recreating index for collection '{collection_name}' with dimension {vector_dimension}")

            # Delete existing index
            self.delete_collection(collection_name)

            # Wait a moment for deletion to complete
            time.sleep(3)

            # Create new index with correct dimension
            self._create_index_if_not_exists(collection_name, vector_dimension)

        except Exception as e:
            log.error(f"Error recreating index: {e}")
            raise

    def search(
        self, collection_name: str, vectors: List[List[float]], limit: int
    ) -> Optional[SearchResult]:
        """Search for the nearest neighbor items based on the vectors"""
        try:
            index_name = self._get_index_name(collection_name)

            if not self.has_collection(collection_name):
                log.warning(f"Collection {collection_name} does not exist")
                return None

            # Use the first vector for search (Azure AI Search doesn't support batch vector search in the same way)
            search_vector = vectors[0] if vectors else []

            search_payload = {
                "search": "*",
                "vectorQueries": [
                    {
                        "vector": search_vector,
                        "fields": "contentVector",
                        "kind": "vector",
                        "k": limit
                    }
                ],
                "select": "id,content,metadata",
                "top": limit
            }

            url = self._get_search_url(index_name)
            response = self.client.post(url, json=search_payload)

            if response.status_code == 200:
                result = response.json()

                ids = []
                documents = []
                metadatas = []
                distances = []

                for doc in result.get("value", []):
                    ids.append(doc["id"])
                    documents.append(doc["content"])

                    # Parse metadata JSON string back to dict
                    try:
                        metadata = json.loads(doc.get("metadata", "{}"))
                    except:
                        metadata = {}
                    metadatas.append(metadata)

                    # Azure AI Search returns scores, convert to distances (1 - score for cosine similarity)
                    score = doc.get("@search.score", 0)
                    distance = 1 - score if score > 0 else 1.0
                    distances.append(distance)

                return SearchResult(
                    ids=[ids],
                    documents=[documents],
                    metadatas=[metadatas],
                    distances=[distances]
                )
            else:
                log.error(f"Search failed: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            log.error(f"Error during search: {e}")
            return None

    def query(
        self, collection_name: str, filter: Dict, limit: Optional[int] = None
    ) -> Optional[GetResult]:
        """Query the items from the collection based on the filter"""
        try:
            index_name = self._get_index_name(collection_name)
            
            log.info(f"Azure AI Search: query called with collection_name='{collection_name}', filter={filter}")

            if not self.has_collection(collection_name):
                log.warning(f"Collection {collection_name} does not exist")
                return None

            # Use efficient search to find documents matching the filter
            log.info(f"Efficiently querying documents with filter: {filter}")
            
            documents_found, total_searched = self._search_documents_efficiently(index_name, filter, "id,content,metadata", limit or 1000)

            ids = []
            documents = []
            metadatas = []

            for doc in documents_found:
                try:
                    doc_id = doc.get("id", "")
                    content = doc.get("content", "")
                    metadata_str = doc.get("metadata", "{}")
                    metadata = json.loads(metadata_str) if metadata_str else {}
                    
                    log.debug(f"Checking document {doc_id} with metadata: {metadata}")
                    
                    # Double-check that this document actually matches our filter
                    if self._matches_filter(metadata, filter):
                        ids.append(doc_id)
                        documents.append(content)
                        metadatas.append(metadata)
                        log.info(f"Document {doc_id} matches filter - included in results")
                    else:
                        log.debug(f"Document {doc_id} does not match filter")
                        
                except Exception as e:
                    log.error(f"Error parsing metadata for document {doc.get('id')}: {e}")
                    log.error(f"Metadata string was: {doc.get('metadata', 'None')}")

            log.info(f"Query found {len(ids)} matching documents out of {total_searched} searched")
            
            if ids:
                return GetResult(
                    ids=[ids],
                    documents=[documents],
                    metadatas=[metadatas]
                )
            else:
                return None

        except Exception as e:
            log.error(f"Error during query: {e}")
            return None

    def get(self, collection_name: str) -> Optional[GetResult]:
        """Get all the items in the collection"""
        try:
            index_name = self._get_index_name(collection_name)

            if not self.has_collection(collection_name):
                log.warning(f"Collection {collection_name} does not exist")
                return None

            search_payload = {
                "search": "*",
                "select": "id,content,metadata",
                "top": 1000  # Azure AI Search default max
            }

            url = self._get_search_url(index_name)
            response = self.client.post(url, json=search_payload)

            if response.status_code == 200:
                result = response.json()

                ids = []
                documents = []
                metadatas = []

                for doc in result.get("value", []):
                    ids.append(doc["id"])
                    documents.append(doc["content"])

                    try:
                        metadata = json.loads(doc.get("metadata", "{}"))
                    except:
                        metadata = {}
                    metadatas.append(metadata)

                return GetResult(
                    ids=[ids],
                    documents=[documents],
                    metadatas=[metadatas]
                )
            else:
                log.error(f"Get all failed: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            log.error(f"Error getting all documents: {e}")
            return None

    def insert(self, collection_name: str, items: List[VectorItem]):
        """Insert the items into the collection"""
        try:
            # 自動偵測向量維度
            vector_dimension = self._detect_vector_dimension(items)
            log.info(f"Detected vector dimension: {vector_dimension}")

            # 創建索引時使用正確的向量維度
            self._create_index_if_not_exists(collection_name, vector_dimension)
            index_name = self._get_index_name(collection_name)

            # Prepare documents for upload
            documents = []
            for item in items:
                doc = {
                    "id": item["id"],
                    "content": item["text"],
                    "contentVector": item["vector"],
                    "metadata": json.dumps(item["metadata"]) if item["metadata"] else "{}"
                }
                documents.append(doc)

            # Upload documents
            upload_payload = {
                "value": [
                    {**doc, "@search.action": "upload"}
                    for doc in documents
                ]
            }

            url = f"{self.endpoint}/indexes/{index_name}/docs/index?api-version={self.api_version}"
            response = self.client.post(url, json=upload_payload)

            if response.status_code in [200, 201]:
                result = response.json()
                log.info(f"Successfully inserted {len(documents)} documents into {index_name}")
                return result
            else:
                log.error(f"Insert failed: {response.status_code} - {response.text}")
                raise Exception(f"Insert failed: {response.text}")

        except Exception as e:
            log.error(f"Error during insert: {e}")
            raise

    def upsert(self, collection_name: str, items: List[VectorItem]):
        """Update the items in the collection, if the items are not present, insert them"""
        try:
            # 自動偵測向量維度
            vector_dimension = self._detect_vector_dimension(items)
            log.info(f"Detected vector dimension: {vector_dimension}")

            # 創建索引時使用正確的向量維度
            self._create_index_if_not_exists(collection_name, vector_dimension)
            index_name = self._get_index_name(collection_name)

            # Prepare documents for upsert
            documents = []
            for item in items:
                doc = {
                    "id": item["id"],
                    "content": item["text"],
                    "contentVector": item["vector"],
                    "metadata": json.dumps(item["metadata"]) if item["metadata"] else "{}"
                }
                documents.append(doc)

            # Upload/merge documents
            upload_payload = {
                "value": [
                    {**doc, "@search.action": "mergeOrUpload"}
                    for doc in documents
                ]
            }

            url = f"{self.endpoint}/indexes/{index_name}/docs/index?api-version={self.api_version}"
            response = self.client.post(url, json=upload_payload)

            if response.status_code in [200, 201]:
                result = response.json()
                log.info(f"Successfully upserted {len(documents)} documents into {index_name}")
                return result
            else:
                log.error(f"Upsert failed: {response.status_code} - {response.text}")
                raise Exception(f"Upsert failed: {response.text}")

        except Exception as e:
            log.error(f"Error during upsert: {e}")
            raise

    def delete(
        self,
        collection_name: str,
        ids: Optional[List[str]] = None,
        filter: Optional[Dict] = None,
    ):
        """Delete the items from the collection based on the ids or filter"""
        try:
            index_name = self._get_index_name(collection_name)
            
            log.info(f"Azure AI Search: delete called with collection_name='{collection_name}', index_name='{index_name}', ids={ids}, filter={filter}")

            if not self.has_collection(collection_name):
                log.warning(f"Collection {collection_name} does not exist")
                return

            if ids:
                # Delete by IDs
                log.info(f"Deleting documents by IDs: {ids}")
                delete_payload = {
                    "value": [
                        {"id": doc_id, "@search.action": "delete"}
                        for doc_id in ids
                    ]
                }

                url = f"{self.endpoint}/indexes/{index_name}/docs/index?api-version={self.api_version}"
                response = self.client.post(url, json=delete_payload)

                if response.status_code in [200, 201]:
                    log.info(f"Successfully deleted documents with IDs: {ids}")
                    return True
                else:
                    log.error(f"Delete by IDs failed: {response.status_code} - {response.text}")
                    return False

            elif filter:
                # For filter-based deletion, we need to first query the documents then delete them
                log.info(f"Deleting documents by filter: {filter}")
                
                # Use a more direct search approach for finding documents with the filter
                documents_to_delete = []
                
                # Use efficient search to find documents matching the filter
                log.info(f"Efficiently searching for documents to delete with filter: {filter}")
                
                documents, total_searched = self._search_documents_efficiently(index_name, filter, "id,metadata", 1000)
                
                for doc in documents:
                    try:
                        doc_id = doc.get("id", "")
                        metadata_str = doc.get("metadata", "{}")
                        metadata = json.loads(metadata_str) if metadata_str else {}
                        
                        log.debug(f"Checking document {doc_id} with metadata: {metadata}")
                        
                        if self._matches_filter(metadata, filter):
                            documents_to_delete.append(doc_id)
                            log.info(f"Document {doc_id} matches filter - will be deleted")
                        else:
                            log.debug(f"Document {doc_id} does not match filter")
                            
                    except Exception as e:
                        log.error(f"Error parsing metadata for document {doc.get('id')}: {e}")
                        log.error(f"Metadata string was: {doc.get('metadata', 'None')}")
                
                log.info(f"Found {len(documents_to_delete)} documents to delete out of {total_searched} searched: {documents_to_delete}")
                
                if documents_to_delete:
                    # Now delete the found documents
                    return self.delete(collection_name, ids=documents_to_delete)
                else:
                    log.info("No documents found matching the filter")
                    return True
            else:
                log.warning("No ids or filter provided for delete operation")
                return False

        except Exception as e:
            log.error(f"Error during delete: {e}")
            raise  # Don't suppress the exception, let it propagate

    def _matches_filter(self, metadata: Dict, filter: Dict) -> bool:
        """Check if metadata matches the given filter"""
        for key, value in filter.items():
            if key not in metadata or metadata[key] != value:
                return False
        return True

    def _search_documents_efficiently(self, index_name: str, filter: Dict, select_fields: str = "id,metadata", limit: int = 1000):
        """
        Efficiently search for documents using multiple strategies
        Returns: (documents, total_searched)
        """
        url = self._get_search_url(index_name)
        
        # Strategy 1: Try OData filter first (most efficient)
        if "file_id" in filter:
            file_id = filter["file_id"]
            
            # Try exact match in metadata
            for filter_expression in [
                f"search.ismatch('{file_id}', 'metadata')",
                f"search.ismatchscoring('{file_id}', 'metadata')",
                f"contains(metadata, '{file_id}')"  # If contains function is available
            ]:
                try:
                    search_payload = {
                        "search": "*",
                        "filter": filter_expression,
                        "select": select_fields,
                        "top": limit
                    }
                    
                    response = self.client.post(url, json=search_payload)
                    if response.status_code == 200:
                        result = response.json()
                        documents = result.get('value', [])
                        if documents:
                            log.info(f"OData filter '{filter_expression}' found {len(documents)} documents")
                            return documents, len(documents)
                except Exception as e:
                    log.debug(f"OData filter '{filter_expression}' failed: {e}")
                    continue
            
            # Strategy 2: Try targeted search in metadata field
            try:
                search_payload = {
                    "search": file_id,
                    "searchFields": "metadata",
                    "select": select_fields,
                    "top": limit
                }
                
                response = self.client.post(url, json=search_payload)
                if response.status_code == 200:
                    result = response.json()
                    documents = result.get('value', [])
                    if documents:
                        log.info(f"Targeted metadata search found {len(documents)} documents")
                        return documents, len(documents)
            except Exception as e:
                log.debug(f"Targeted metadata search failed: {e}")
        
        # Strategy 3: Fallback to paginated full search (last resort)
        log.warning("Using fallback paginated search - this may be slow for large indexes")
        all_documents = []
        skip = 0
        batch_size = min(1000, limit)
        total_searched = 0
        
        while total_searched < 10000:  # Safety limit to prevent infinite loops
            search_payload = {
                "search": "*",
                "select": select_fields,
                "top": batch_size,
                "skip": skip
            }
            
            response = self.client.post(url, json=search_payload)
            if response.status_code != 200:
                break
                
            result = response.json()
            batch_documents = result.get('value', [])
            
            if not batch_documents:
                break
                
            # Filter documents in this batch
            for doc in batch_documents:
                try:
                    metadata_str = doc.get("metadata", "{}")
                    metadata = json.loads(metadata_str) if metadata_str else {}
                    if self._matches_filter(metadata, filter):
                        all_documents.append(doc)
                except Exception as e:
                    log.debug(f"Error parsing metadata for document {doc.get('id')}: {e}")
            
            total_searched += len(batch_documents)
            skip += batch_size
            
            # If we found enough matches or the batch was smaller than expected, stop
            if len(all_documents) >= limit or len(batch_documents) < batch_size:
                break
        
        log.info(f"Paginated search found {len(all_documents)} matching documents out of {total_searched} searched")
        return all_documents, total_searched

    def reset(self):
        """Reset the database by deleting all indexes with the configured prefix"""
        try:
            # List all indexes
            url = f"{self.endpoint}/indexes?api-version={self.api_version}"
            response = self.client.get(url)

            if response.status_code == 200:
                result = response.json()
                indexes_to_delete = []

                for index in result.get("value", []):
                    index_name = index["name"]
                    if index_name.startswith(self.index_prefix):
                        indexes_to_delete.append(index_name)

                # Delete each index
                for index_name in indexes_to_delete:
                    delete_url = f"{self.endpoint}/indexes/{index_name}?api-version={self.api_version}"
                    delete_response = self.client.delete(delete_url)
                    if delete_response.status_code in [200, 204, 404]:
                        log.info(f"Deleted index: {index_name}")
                    else:
                        log.error(f"Failed to delete index {index_name}: {delete_response.text}")

                log.info(f"Reset completed. Deleted {len(indexes_to_delete)} indexes.")
                return True
            else:
                log.error(f"Failed to list indexes: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            log.error(f"Error during reset: {e}")
            return False

    def __del__(self):
        """Clean up HTTP client"""
        if hasattr(self, 'client'):
            try:
                self.client.close()
            except:
                pass
