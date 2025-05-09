import base64
from io import BytesIO
from typing import Dict, Any, List, Optional
from core.models.prompts import QueryPromptOverrides, GraphPromptOverrides
from fastapi import UploadFile
from datetime import datetime, UTC
import torch
from core.models.chunk import Chunk, DocumentChunk
from core.models.documents import (
    Document,
    ChunkResult,
    DocumentContent,
    DocumentResult,
    StorageFileInfo,
)
from ..models.auth import AuthContext
from ..models.graph import Graph
from ..models.folders import Folder
from colpali_engine.models import ColIdefics3, ColIdefics3Processor
from core.services.graph_service import GraphService
from core.database.base_database import BaseDatabase
from core.storage.base_storage import BaseStorage
from core.vector_store.base_vector_store import BaseVectorStore
from core.embedding.base_embedding_model import BaseEmbeddingModel
from core.parser.base_parser import BaseParser
from core.completion.base_completion import BaseCompletionModel
from core.models.completion import CompletionRequest, CompletionResponse, ChunkSource
import logging
from core.reranker.base_reranker import BaseReranker
from core.config import get_settings
from core.cache.base_cache import BaseCache
from core.cache.base_cache_factory import BaseCacheFactory
from core.services.rules_processor import RulesProcessor
from core.embedding.colpali_embedding_model import ColpaliEmbeddingModel
from core.vector_store.multi_vector_store import MultiVectorStore
import filetype
from filetype.types import IMAGE  # , DOCUMENT, document
import pdf2image
from PIL.Image import Image
import tempfile
import os
import asyncio

logger = logging.getLogger(__name__)
IMAGE = {im.mime for im in IMAGE}

CHARS_PER_TOKEN = 4
TOKENS_PER_PAGE = 630

class DocumentService:
    async def _ensure_folder_exists(self, folder_name: str, document_id: str, auth: AuthContext) -> Optional[Folder]:
        """
        Check if a folder exists, if not create it. Also adds the document to the folder.
        
        Args:
            folder_name: Name of the folder
            document_id: ID of the document to add to the folder
            auth: Authentication context
            
        Returns:
            Folder object if found or created, None on error
        """
        try:
            # First check if the folder already exists
            folder = await self.db.get_folder_by_name(folder_name, auth)
            if folder:
                # Add document to existing folder
                if document_id not in folder.document_ids:
                    success = await self.db.add_document_to_folder(folder.id, document_id, auth)
                    if not success:
                        logger.warning(f"Failed to add document {document_id} to existing folder {folder.name}")
                return folder  # Folder already exists
                
            # Create a new folder
            folder = Folder(
                name=folder_name,
                owner={
                    "type": auth.entity_type.value,
                    "id": auth.entity_id,
                },
                document_ids=[document_id],  # Add document_id to the new folder
            )
            
            await self.db.create_folder(folder)
            return folder
            
        except Exception as e:
            # Log error but don't raise - we want document ingestion to continue even if folder creation fails
            logger.error(f"Error ensuring folder exists: {e}")
            return None
    
    def __init__(
        self,
        database: BaseDatabase,
        vector_store: BaseVectorStore,
        storage: BaseStorage,
        parser: BaseParser,
        embedding_model: BaseEmbeddingModel,
        completion_model: Optional[BaseCompletionModel] = None,
        cache_factory: Optional[BaseCacheFactory] = None,
        reranker: Optional[BaseReranker] = None,
        enable_colpali: bool = True,
        colpali_embedding_model: Optional[ColpaliEmbeddingModel] = "vidore/colqwen2.5-v0.2",
        colpali_vector_store: Optional[MultiVectorStore] = None,
    ):
        self.db = database
        self.vector_store = vector_store
        self.storage = storage
        self.parser = parser
        self.embedding_model = embedding_model
        self.completion_model = completion_model
        self.reranker = reranker
        self.cache_factory = cache_factory
        self.rules_processor = RulesProcessor()
        self.colpali_embedding_model = colpali_embedding_model
        self.colpali_vector_store = colpali_vector_store
        
        # Initialize the graph service only if completion_model is provided
        # (e.g., not needed for ingestion worker)
        if completion_model is not None:
            self.graph_service = GraphService(
                db=database,
                embedding_model=embedding_model,
                completion_model=completion_model,
            )
        else:
            self.graph_service = None

        # MultiVectorStore initialization is now handled in the FastAPI startup event
        # so we don't need to initialize it here again

        # Cache-related data structures
        # Maps cache name to active cache object
        self.active_caches: Dict[str, BaseCache] = {}

    async def retrieve_chunks(
        self,
        query: str,
        auth: AuthContext,
        filters: Optional[Dict[str, Any]] = None,
        k: int = 5,
        min_score: float = 0.0,
        use_reranking: Optional[bool] = None,
        use_colpali: Optional[bool] = None,
        folder_name: Optional[str] = None,
        end_user_id: Optional[str] = None,
    ) -> List[ChunkResult]:
        """Retrieve relevant chunks."""

        # 4 configurations:
        # 1. No reranking, no colpali -> just return regular chunks
        # 2. No reranking, colpali  -> return colpali chunks + regular chunks - no need to run smaller colpali model
        # 3. Reranking, no colpali -> sort regular chunks by re-ranker score
        # 4. Reranking, colpali -> return merged chunks sorted by smaller colpali model score
        
        settings = get_settings()
        should_rerank = use_reranking if use_reranking is not None else settings.USE_RERANKING
        using_colpali = use_colpali if use_colpali is not None else False

        # Get embedding for query
        query_embedding_regular = await self.embedding_model.embed_for_query(query)
        query_embedding_multivector = await self.colpali_embedding_model.embed_for_query(query) if (using_colpali and self.colpali_embedding_model) else None
        logger.info("Generated query embedding")

        # Find authorized documents
        # Build system filters for folder_name and end_user_id
        system_filters = {}
        if folder_name:
            system_filters["folder_name"] = folder_name
        if end_user_id:
            system_filters["end_user_id"] = end_user_id
            
        doc_ids = await self.db.find_authorized_and_filtered_documents(auth, filters, system_filters)
        if not doc_ids:
            logger.info("No authorized documents found")
            return []
        logger.info(f"Found {len(doc_ids)} authorized documents")

        # Check if we're using colpali multivector search
        search_multi = using_colpali and self.colpali_vector_store and query_embedding_multivector is not None
        
        # For regular reranking (without colpali), we'll use the existing reranker if available
        # For colpali reranking, we'll handle it in _combine_multi_and_regular_chunks
        use_standard_reranker = should_rerank and (not search_multi) and self.reranker is not None

        # Search chunks with vector similarity
        # When using standard reranker, we get more chunks initially to improve reranking quality
        chunks = await self.vector_store.query_similar(
            query_embedding_regular, k=10 * k if use_standard_reranker else k, doc_ids=doc_ids
        )

        chunks_multivector = (
            await self.colpali_vector_store.query_similar(
                query_embedding_multivector, k=k, doc_ids=doc_ids
            ) if search_multi else []
        )

        logger.debug(f"Found {len(chunks)} similar chunks via regular embedding")
        if using_colpali:
            logger.debug(f"Found {len(chunks_multivector)} similar chunks via multivector embedding since we are also using colpali")

        # Rerank chunks using the standard reranker if enabled and available
        # This handles configuration 3: Reranking without colpali
        if chunks and use_standard_reranker:
            chunks = await self.reranker.rerank(query, chunks)
            chunks.sort(key=lambda x: x.score, reverse=True)
            chunks = chunks[:k]
            logger.debug(f"Reranked {k*10} chunks and selected the top {k}")

        # Combine multiple chunk sources if needed
        chunks = await self._combine_multi_and_regular_chunks(query, chunks, chunks_multivector, should_rerank=should_rerank)

        # Create and return chunk results
        results = await self._create_chunk_results(auth, chunks)
        logger.info(f"Returning {len(results)} chunk results")
        return results

    async def _combine_multi_and_regular_chunks(self, query: str, chunks: List[DocumentChunk], chunks_multivector: List[DocumentChunk], should_rerank: bool = None):
        """Combine and potentially rerank regular and colpali chunks based on configuration.
        
        # 4 configurations:
        # 1. No reranking, no colpali -> just return regular chunks - this already happens upstream, correctly
        # 2. No reranking, colpali  -> return colpali chunks + regular chunks - no need to run smaller colpali model
        # 3. Reranking, no colpali -> sort regular chunks by re-ranker score - this already happens upstream, correctly
        # 4. Reranking, colpali -> return merged chunks sorted by smaller colpali model score
        
        Args:
            query: The user query
            chunks: Regular chunks with embeddings
            chunks_multivector: Colpali multi-vector chunks
            should_rerank: Whether reranking is enabled
        """
        # Handle simple cases first
        if len(chunks_multivector) == 0:
            return chunks
        if len(chunks) == 0:
            return chunks_multivector

        # Use global setting if not provided
        if should_rerank is None:
            settings = get_settings()
            should_rerank = settings.USE_RERANKING
        
        # Check if we need to run the reranking - if reranking is disabled, we just combine the chunks
        # This is Configuration 2: No reranking, with colpali
        if not should_rerank:
            # For configuration 2, simply combine the chunks with multivector chunks first
            # since they are generally higher quality
            logger.debug("Using configuration 2: No reranking, with colpali - combining chunks without rescoring")
            combined_chunks = chunks_multivector + chunks
            return combined_chunks
            
        # Configuration 4: Reranking with colpali
        # Use colpali as a reranker to get consistent similarity scores for both types of chunks
        logger.debug("Using configuration 4: Reranking with colpali - rescoring chunks with colpali model")
        
        model_name = "vidore/colSmol-256M"
        device = (
            "mps"
            if torch.backends.mps.is_available()
            else "cuda" if torch.cuda.is_available() else "cpu"
        )

        model = ColIdefics3.from_pretrained(
            model_name,
            torch_dtype=torch.bfloat16,
            device_map=device,  # "cuda:0",  # or "mps" if on Apple Silicon
            attn_implementation="eager",  # "flash_attention_2" if is_flash_attn_2_available() else None,  # or "eager" if "mps"
        ).eval()
        processor = ColIdefics3Processor.from_pretrained(model_name)

        # Score regular chunks with colpali model for consistent comparison
        batch_chunks = processor.process_queries([chunk.content for chunk in chunks]).to(device)
        query_rep = processor.process_queries([query]).to(device)
        multi_vec_representations = model(**batch_chunks)
        query_rep = model(**query_rep)
        scores = processor.score_multi_vector(query_rep, multi_vec_representations)
        for chunk, score in zip(chunks, scores[0]):
            chunk.score = score
            
        # Combine and sort all chunks
        full_chunks = chunks + chunks_multivector
        full_chunks.sort(key=lambda x: x.score, reverse=True)
        return full_chunks

    async def retrieve_docs(
        self,
        query: str,
        auth: AuthContext,
        filters: Optional[Dict[str, Any]] = None,
        k: int = 5,
        min_score: float = 0.0,
        use_reranking: Optional[bool] = None,
        use_colpali: Optional[bool] = None,
        folder_name: Optional[str] = None,
        end_user_id: Optional[str] = None,
    ) -> List[DocumentResult]:
        """Retrieve relevant documents."""
        # Get chunks first
        chunks = await self.retrieve_chunks(
            query, auth, filters, k, min_score, use_reranking, use_colpali, folder_name, end_user_id
        )
        # Convert to document results
        results = await self._create_document_results(auth, chunks)
        documents = list(results.values())
        logger.info(f"Returning {len(documents)} document results")
        return documents
        
    async def batch_retrieve_documents(
        self,
        document_ids: List[str],
        auth: AuthContext,
        folder_name: Optional[str] = None,
        end_user_id: Optional[str] = None
    ) -> List[Document]:
        """
        Retrieve multiple documents by their IDs in a single batch operation.
        
        Args:
            document_ids: List of document IDs to retrieve
            auth: Authentication context
            
        Returns:
            List of Document objects that user has access to
        """
        if not document_ids:
            return []
            
        # Build system filters for folder_name and end_user_id
        system_filters = {}
        if folder_name:
            system_filters["folder_name"] = folder_name
        if end_user_id:
            system_filters["end_user_id"] = end_user_id
            
        # Use the database's batch retrieval method
        documents = await self.db.get_documents_by_id(document_ids, auth, system_filters)
        logger.info(f"Batch retrieved {len(documents)} documents out of {len(document_ids)} requested")
        return documents
        
    async def batch_retrieve_chunks(
        self,
        chunk_ids: List[ChunkSource],
        auth: AuthContext,
        folder_name: Optional[str] = None,
        end_user_id: Optional[str] = None,
        use_colpali: Optional[bool] = None
    ) -> List[ChunkResult]:
        """
        Retrieve specific chunks by their document ID and chunk number in a single batch operation.
        
        Args:
            chunk_ids: List of ChunkSource objects with document_id and chunk_number
            auth: Authentication context
            folder_name: Optional folder to scope the operation to
            end_user_id: Optional end-user ID to scope the operation to
            use_colpali: Whether to use colpali multimodal features for image chunks
            
        Returns:
            List of ChunkResult objects
        """
        if not chunk_ids:
            return []
            
        # Collect unique document IDs to check authorization in a single query
        doc_ids = list({source.document_id for source in chunk_ids})
        
        # Find authorized documents in a single query
        authorized_docs = await self.batch_retrieve_documents(doc_ids, auth, folder_name, end_user_id)
        authorized_doc_ids = {doc.external_id for doc in authorized_docs}
        
        # Filter sources to only include authorized documents
        authorized_sources = [
            source for source in chunk_ids 
            if source.document_id in authorized_doc_ids
        ]
        
        if not authorized_sources:
            return []
            
        # Create list of (document_id, chunk_number) tuples for vector store query
        chunk_identifiers = [
            (source.document_id, source.chunk_number) 
            for source in authorized_sources
        ]
        
        # Retrieve the chunks from vector store in a single query
        chunks = await self.vector_store.get_chunks_by_id(chunk_identifiers)
        
        # Check if we should use colpali for image chunks
        if use_colpali and self.colpali_vector_store:
            logger.info("Trying to retrieve chunks from colpali vector store")
            try:
                # Also try to retrieve from the colpali vector store
                colpali_chunks = await self.colpali_vector_store.get_chunks_by_id(chunk_identifiers)
                
                if colpali_chunks:
                    # Create a dictionary of (doc_id, chunk_number) -> chunk for fast lookup
                    chunk_dict = {(c.document_id, c.chunk_number): c for c in chunks}
                    
                    logger.debug(f"Found {len(colpali_chunks)} chunks in colpali store")
                    for colpali_chunk in colpali_chunks:
                        logger.debug(f"Found colpali chunk: doc={colpali_chunk.document_id}, chunk={colpali_chunk.chunk_number}")
                        key = (colpali_chunk.document_id, colpali_chunk.chunk_number)
                        # Replace chunks with colpali chunks when available
                        chunk_dict[key] = colpali_chunk
                    
                    # Update chunks list with the combined/replaced chunks
                    chunks = list(chunk_dict.values())
                    logger.info(f"Enhanced {len(colpali_chunks)} chunks with colpali/multimodal data")
                else:
                    logger.warning("No chunks found in colpali vector store")
            except Exception as e:
                logger.error(f"Error retrieving chunks from colpali vector store: {e}", exc_info=True)
        
        # Convert to chunk results
        results = await self._create_chunk_results(auth, chunks)
        logger.info(f"Batch retrieved {len(results)} chunks out of {len(chunk_ids)} requested")
        return results

    async def query(
        self,
        query: str,
        auth: AuthContext,
        filters: Optional[Dict[str, Any]] = None,
        k: int = 20,  # from contextual embedding paper
        min_score: float = 0.0,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        use_reranking: Optional[bool] = None,
        use_colpali: Optional[bool] = None,
        graph_name: Optional[str] = None,
        hop_depth: int = 1,
        include_paths: bool = False,
        prompt_overrides: Optional["QueryPromptOverrides"] = None,
        folder_name: Optional[str] = None,
        end_user_id: Optional[str] = None,
    ) -> CompletionResponse:
        """Generate completion using relevant chunks as context.
        
        When graph_name is provided, the query will leverage the knowledge graph 
        to enhance retrieval by finding relevant entities and their connected documents.
        
        Args:
            query: The query text
            auth: Authentication context
            filters: Optional metadata filters for documents
            k: Number of chunks to retrieve
            min_score: Minimum similarity score
            max_tokens: Maximum tokens for completion
            temperature: Temperature for completion
            use_reranking: Whether to use reranking
            use_colpali: Whether to use colpali embedding
            graph_name: Optional name of the graph to use for knowledge graph-enhanced retrieval
            hop_depth: Number of relationship hops to traverse in the graph (1-3)
            include_paths: Whether to include relationship paths in the response
        """
        if graph_name:
            # Use knowledge graph enhanced retrieval via GraphService
            return await self.graph_service.query_with_graph(
                query=query,
                graph_name=graph_name,
                auth=auth,
                document_service=self,
                filters=filters,
                k=k,
                min_score=min_score,
                max_tokens=max_tokens,
                temperature=temperature,
                use_reranking=use_reranking,
                use_colpali=use_colpali,
                hop_depth=hop_depth,
                include_paths=include_paths,
                prompt_overrides=prompt_overrides,
                folder_name=folder_name,
                end_user_id=end_user_id
            )
        
        # Standard retrieval without graph
        chunks = await self.retrieve_chunks(
            query, auth, filters, k, min_score, use_reranking, use_colpali, folder_name, end_user_id
        )
        documents = await self._create_document_results(auth, chunks)

        # Create augmented chunk contents
        chunk_contents = [chunk.augmented_content(documents[chunk.document_id]) for chunk in chunks]
        
        # Collect sources information
        sources = [
            ChunkSource(document_id=chunk.document_id, chunk_number=chunk.chunk_number, score=chunk.score)
            for chunk in chunks
        ]

        # Generate completion with prompt override if provided
        custom_prompt_template = None
        if prompt_overrides and prompt_overrides.query:
            custom_prompt_template = prompt_overrides.query.prompt_template
            
        request = CompletionRequest(
            query=query,
            context_chunks=chunk_contents,
            max_tokens=max_tokens,
            temperature=temperature,
            prompt_template=custom_prompt_template,
        )

        response = await self.completion_model.complete(request)
        
        # Add sources information at the document service level
        response.sources = sources
        
        return response

    async def ingest_text(
        self,
        content: str,
        filename: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        auth: AuthContext = None,
        rules: Optional[List[str]] = None,
        use_colpali: Optional[bool] = None,
        folder_name: Optional[str] = None,
        end_user_id: Optional[str] = None,
    ) -> Document:
        """Ingest a text document."""
        if "write" not in auth.permissions:
            logger.error(f"User {auth.entity_id} does not have write permission")
            raise PermissionError("User does not have write permission")

        # First check ingest limits if in cloud mode
        from core.config import get_settings
        settings = get_settings()
        
        doc = Document(
            content_type="text/plain",
            filename=filename,
            metadata=metadata or {},
            owner={"type": auth.entity_type, "id": auth.entity_id},
            access_control={
                "readers": [auth.entity_id],
                "writers": [auth.entity_id],
                "admins": [auth.entity_id],
                "user_id": [auth.user_id] if auth.user_id else [],  # Add user_id to access control for filtering (as a list)
            },
        )
        
        # Add folder_name and end_user_id to system_metadata if provided
        if folder_name:
            doc.system_metadata["folder_name"] = folder_name
            
            # Check if the folder exists, if not create it
            await self._ensure_folder_exists(folder_name, doc.external_id, auth)
            
        if end_user_id:
            doc.system_metadata["end_user_id"] = end_user_id
        logger.debug(f"Created text document record with ID {doc.external_id}")

        if settings.MODE == "cloud" and auth.user_id:
            # Check limits before proceeding
            from core.api import check_and_increment_limits
            num_pages = int(len(content)/(CHARS_PER_TOKEN*TOKENS_PER_PAGE)) # 
            await check_and_increment_limits(auth, "ingest", num_pages, doc.external_id)

        # Apply rules if provided
        if rules:
            rule_metadata, modified_text = await self.rules_processor.process_rules(content, rules)
            # Update document metadata with extracted metadata from rules
            metadata.update(rule_metadata)
            doc.metadata = metadata  # Update doc metadata after rules

            if modified_text:
                content = modified_text
                logger.info("Updated content with modified text from rules")

        # Store full content before chunking
        doc.system_metadata["content"] = content

        # Split into chunks after all processing is done
        chunks = await self.parser.split_text(content)
        if not chunks:
            raise ValueError("No content chunks extracted")
        logger.debug(f"Split processed text into {len(chunks)} chunks")

        # Generate embeddings for chunks
        embeddings = await self.embedding_model.embed_for_ingestion(chunks)
        logger.debug(f"Generated {len(embeddings)} embeddings")
        chunk_objects = self._create_chunk_objects(doc.external_id, chunks, embeddings)
        logger.debug(f"Created {len(chunk_objects)} chunk objects")

        chunk_objects_multivector = []

        if use_colpali and self.colpali_embedding_model:
            embeddings_multivector = await self.colpali_embedding_model.embed_for_ingestion(chunks)
            logger.info(
                f"Generated {len(embeddings_multivector)} embeddings for multivector embedding"
            )
            chunk_objects_multivector = self._create_chunk_objects(
                doc.external_id, chunks, embeddings_multivector
            )
            logger.info(
                f"Created {len(chunk_objects_multivector)} chunk objects for multivector embedding"
            )

        # Create and store chunk objects

        # Store everything
        await self._store_chunks_and_doc(chunk_objects, doc, use_colpali, chunk_objects_multivector)
        logger.debug(f"Successfully stored text document {doc.external_id}")

        return doc
    
    # TODO: check if it's unused. if so, remove it.
    async def ingest_file(
        self,
        file: UploadFile,
        metadata: Dict[str, Any],
        auth: AuthContext,
        rules: Optional[List[str]] = None,
        use_colpali: Optional[bool] = None,
        folder_name: Optional[str] = None,
        end_user_id: Optional[str] = None,
    ) -> Document:
        """Ingest a file document."""
        if "write" not in auth.permissions:
            raise PermissionError("User does not have write permission")

        # Read file content
        file_content = await file.read()
        file_size = len(file_content)  # Get file size in bytes for limit checking
        
        # Check limits before doing any expensive processing
        from core.config import get_settings
        settings = get_settings()
        
        if settings.MODE == "cloud" and auth.user_id:
            # Check limits before proceeding with parsing
            from core.api import check_and_increment_limits
            await check_and_increment_limits(auth, "storage_file", 1)
            await check_and_increment_limits(auth, "storage_size", file_size)
            
        # Now proceed with parsing and processing the file
        file_type = filetype.guess(file_content)
        
        # Set default mime type for cases where filetype.guess returns None
        mime_type = ""
        if file_type is not None:
            mime_type = file_type.mime
        elif file.filename:
            # Try to determine by file extension as fallback
            import mimetypes
            guessed_type = mimetypes.guess_type(file.filename)[0]
            if guessed_type:
                mime_type = guessed_type
            else:
                # Default for text files
                mime_type = "text/plain"
        else:
            mime_type = "application/octet-stream"  # Generic binary data
            
        logger.info(f"Determined MIME type: {mime_type} for file {file.filename}")

        # Parse file to text first
        additional_metadata, text = await self.parser.parse_file_to_text(
            file_content, file.filename
        )
        logger.debug(f"Parsed file into text of length {len(text)}")

        # Apply rules if provided
        if rules:
            rule_metadata, modified_text = await self.rules_processor.process_rules(text, rules)
            # Update document metadata with extracted metadata from rules
            metadata.update(rule_metadata)
            if modified_text:
                text = modified_text
                logger.info("Updated text with modified content from rules")
        
        doc = Document(
            content_type=mime_type,
            filename=file.filename,
            metadata=metadata,
            owner={"type": auth.entity_type, "id": auth.entity_id},
            access_control={
                "readers": [auth.entity_id],
                "writers": [auth.entity_id],
                "admins": [auth.entity_id],
                "user_id": [auth.user_id] if auth.user_id else [],  # Add user_id to access control for filtering (as a list)
            },
            additional_metadata=additional_metadata,
        )
        
        # Add folder_name and end_user_id to system_metadata if provided
        if folder_name:
            doc.system_metadata["folder_name"] = folder_name
            
            # Check if the folder exists, if not create it
            await self._ensure_folder_exists(folder_name, doc.external_id, auth)
            
        if end_user_id:
            doc.system_metadata["end_user_id"] = end_user_id

        if settings.MODE == "cloud" and auth.user_id:
            # Check limits before proceeding with parsing
            from core.api import check_and_increment_limits
            num_pages = int(len(text)/(CHARS_PER_TOKEN*TOKENS_PER_PAGE)) # 
            await check_and_increment_limits(auth, "ingest", num_pages, doc.external_id)

        # Store full content
        doc.system_metadata["content"] = text
        logger.debug(f"Created file document record with ID {doc.external_id}")

        file_content_base64 = base64.b64encode(file_content).decode()
        # Store the original file
        storage_info = await self.storage.upload_from_base64(
            file_content_base64, doc.external_id, file.content_type
        )
        doc.storage_info = {"bucket": storage_info[0], "key": storage_info[1]}
        logger.debug(f"Stored file in bucket `{storage_info[0]}` with key `{storage_info[1]}`")

        # Split into chunks after all processing is done
        chunks = await self.parser.split_text(text)
        if not chunks:
            raise ValueError("No content chunks extracted")
        logger.debug(f"Split processed text into {len(chunks)} chunks")

        # Generate embeddings for chunks
        embeddings = await self.embedding_model.embed_for_ingestion(chunks)
        logger.debug(f"Generated {len(embeddings)} embeddings")

        # Create and store chunk objects
        chunk_objects = self._create_chunk_objects(doc.external_id, chunks, embeddings)
        logger.debug(f"Created {len(chunk_objects)} chunk objects")

        chunk_objects_multivector = []
        logger.debug(f"use_colpali: {use_colpali}")
        if use_colpali and self.colpali_embedding_model:
            chunks_multivector = self._create_chunks_multivector(
                file_type, file_content_base64, file_content, chunks
            )
            logger.debug(f"Created {len(chunks_multivector)} chunks for multivector embedding")
            colpali_embeddings = await self.colpali_embedding_model.embed_for_ingestion(
                chunks_multivector
            )
            logger.debug(f"Generated {len(colpali_embeddings)} embeddings for multivector embedding")
            chunk_objects_multivector = self._create_chunk_objects(
                doc.external_id, chunks_multivector, colpali_embeddings
            )

        # Store everything
        doc.chunk_ids = await self._store_chunks_and_doc(
            chunk_objects, doc, use_colpali, chunk_objects_multivector
        )
        logger.debug(f"Successfully stored file document {doc.external_id}")

        return doc

    def img_to_base64_str(self, img: Image):
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        buffered.seek(0)
        img_byte = buffered.getvalue()
        img_str = "data:image/png;base64," + base64.b64encode(img_byte).decode()
        return img_str

    def _create_chunks_multivector(
        self, file_type, file_content_base64: str, file_content: bytes, chunks: List[Chunk]
    ):
        # Handle the case where file_type is None
        mime_type = file_type.mime if file_type is not None else "text/plain"
        logger.info(f"Creating chunks for multivector embedding for file type {mime_type}")
        
        # If file_type is None, treat it as a text file
        if file_type is None:
            logger.info("File type is None, treating as text")
            return [
                Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                for chunk in chunks
            ]
            
        match mime_type:
            case file_type if file_type in IMAGE:
                return [Chunk(content=file_content_base64, metadata={"is_image": True})]
            case "application/pdf":
                logger.info("Working with PDF file!")
                images = pdf2image.convert_from_bytes(file_content)
                images_b64 = [self.img_to_base64_str(image) for image in images]
                return [
                    Chunk(content=image_b64, metadata={"is_image": True})
                    for image_b64 in images_b64
                ]
            case "application/vnd.openxmlformats-officedocument.wordprocessingml.document" | "application/msword":
                logger.info("Working with Word document!")
                # Check if file content is empty
                if not file_content or len(file_content) == 0:
                    logger.error("Word document content is empty")
                    return [
                        Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                        for chunk in chunks
                    ]
                
                # Convert Word document to PDF first
                with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as temp_docx:
                    temp_docx.write(file_content)
                    temp_docx_path = temp_docx.name
                
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_pdf:
                    temp_pdf_path = temp_pdf.name
                
                try:
                    # Convert Word to PDF
                    import subprocess
                    
                    # Get the base filename without extension
                    base_filename = os.path.splitext(os.path.basename(temp_docx_path))[0]
                    output_dir = os.path.dirname(temp_pdf_path)
                    expected_pdf_path = os.path.join(output_dir, f"{base_filename}.pdf")
                    
                    result = subprocess.run(
                        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", 
                         output_dir, temp_docx_path],
                        capture_output=True,
                        text=True
                    )
                    
                    if result.returncode != 0:
                        logger.error(f"Failed to convert Word to PDF: {result.stderr}")
                        return [
                            Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                            for chunk in chunks
                        ]
                    
                    # LibreOffice creates the PDF with the same base name in the output directory
                    # Check if the expected PDF file exists
                    if not os.path.exists(expected_pdf_path) or os.path.getsize(expected_pdf_path) == 0:
                        logger.error(f"Generated PDF is empty or doesn't exist at expected path: {expected_pdf_path}")
                        return [
                            Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                            for chunk in chunks
                        ]
                    
                    # Now process the PDF using the correct path
                    with open(expected_pdf_path, "rb") as pdf_file:
                        pdf_content = pdf_file.read()
                    
                    try:
                        images = pdf2image.convert_from_bytes(pdf_content)
                        if not images:
                            logger.warning("No images extracted from PDF")
                            return [
                                Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                                for chunk in chunks
                            ]
                        
                        images_b64 = [self.img_to_base64_str(image) for image in images]
                        return [
                            Chunk(content=image_b64, metadata={"is_image": True})
                            for image_b64 in images_b64
                        ]
                    except Exception as pdf_error:
                        logger.error(f"Error converting PDF to images: {str(pdf_error)}")
                        return [
                            Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                            for chunk in chunks
                        ]
                except Exception as e:
                    logger.error(f"Error processing Word document: {str(e)}")
                    return [
                        Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                        for chunk in chunks
                    ]
                finally:
                    # Clean up temporary files
                    if os.path.exists(temp_docx_path):
                        os.unlink(temp_docx_path)
                    if os.path.exists(temp_pdf_path):
                        os.unlink(temp_pdf_path)
                    # Also clean up the expected PDF path if it exists and is different from temp_pdf_path
                    if 'expected_pdf_path' in locals() and os.path.exists(expected_pdf_path) and expected_pdf_path != temp_pdf_path:
                        os.unlink(expected_pdf_path)

            # case filetype.get_type(ext="txt"):
            #     logger.info(f"Found text input: chunks for multivector embedding")
            #     return chunks.copy()
            # TODO: Add support for office documents
            # case document.Xls | document.Xlsx | document.Ods |document.Odp:
            #     logger.warning(f"Colpali is not supported for file type {file_type.mime} - skipping")
            # case file_type if file_type in DOCUMENT:
            #     pass
            case _:
                logger.warning(
                    f"Colpali is not supported for file type {file_type.mime} - skipping"
                )
                return [
                    Chunk(content=chunk.content, metadata=(chunk.metadata | {"is_image": False}))
                    for chunk in chunks
                ]

    def _create_chunk_objects(
        self,
        doc_id: str,
        chunks: List[Chunk],
        embeddings: List[List[float]],
    ) -> List[DocumentChunk]:
        """Helper to create chunk objects
        
        Note: folder_name and end_user_id are not needed in chunk metadata because:
        1. Filtering by these values happens at the document level in find_authorized_and_filtered_documents
        2. Vector search is only performed on already authorized and filtered documents
        3. This approach is more efficient as it reduces the size of chunk metadata
        """
        return [
            c.to_document_chunk(chunk_number=i, embedding=embedding, document_id=doc_id)
            for i, (embedding, c) in enumerate(zip(embeddings, chunks))
        ]

    async def _store_chunks_and_doc(
        self,
        chunk_objects: List[DocumentChunk],
        doc: Document,
        use_colpali: bool = False,
        chunk_objects_multivector: Optional[List[DocumentChunk]] = None,
        is_update: bool = False,
        auth: Optional[AuthContext] = None,
    ) -> List[str]:
        """Helper to store chunks and document"""
        # Add retry logic for vector store operations
        max_retries = 3
        retry_delay = 1.0
        
        # Store chunks in vector store with retry
        attempt = 0
        success = False
        result = None
        
        while attempt < max_retries and not success:
            try:
                success, result = await self.vector_store.store_embeddings(chunk_objects)
                if not success:
                    raise Exception("Failed to store chunk embeddings")
                break
            except Exception as e:
                attempt += 1
                error_msg = str(e)
                if "connection was closed" in error_msg or "ConnectionDoesNotExistError" in error_msg:
                    if attempt < max_retries:
                        logger.warning(f"Database connection error during embeddings storage (attempt {attempt}/{max_retries}): {error_msg}. Retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        # Increase delay for next retry (exponential backoff)
                        retry_delay *= 2
                    else:
                        logger.error(f"All database connection attempts failed after {max_retries} retries: {error_msg}")
                        raise Exception("Failed to store chunk embeddings after multiple retries")
                else:
                    # For other exceptions, don't retry
                    logger.error(f"Error storing embeddings: {error_msg}")
                    raise
        
        logger.debug("Stored chunk embeddings in vector store")
        doc.chunk_ids = result

        if use_colpali and self.colpali_vector_store and chunk_objects_multivector:
            # Reset retry variables for colpali storage
            attempt = 0
            retry_delay = 1.0
            success = False
            result_multivector = None
            
            while attempt < max_retries and not success:
                try:
                    success, result_multivector = await self.colpali_vector_store.store_embeddings(
                        chunk_objects_multivector
                    )
                    if not success:
                        raise Exception("Failed to store multivector chunk embeddings")
                    break
                except Exception as e:
                    attempt += 1
                    error_msg = str(e)
                    if "connection was closed" in error_msg or "ConnectionDoesNotExistError" in error_msg:
                        if attempt < max_retries:
                            logger.warning(f"Database connection error during colpali embeddings storage (attempt {attempt}/{max_retries}): {error_msg}. Retrying in {retry_delay}s...")
                            await asyncio.sleep(retry_delay)
                            # Increase delay for next retry (exponential backoff)
                            retry_delay *= 2
                        else:
                            logger.error(f"All colpali database connection attempts failed after {max_retries} retries: {error_msg}")
                            raise Exception("Failed to store multivector chunk embeddings after multiple retries")
                    else:
                        # For other exceptions, don't retry
                        logger.error(f"Error storing colpali embeddings: {error_msg}")
                        raise
            
            logger.debug("Stored multivector chunk embeddings in vector store")
            doc.chunk_ids += result_multivector

        # Store document metadata with retry
        attempt = 0
        retry_delay = 1.0
        success = False
        
        while attempt < max_retries and not success:
            try:
                if is_update and auth:
                    # For updates, use update_document, serialize StorageFileInfo into plain dicts
                    updates = {
                        "chunk_ids": doc.chunk_ids,
                        "metadata": doc.metadata,
                        "system_metadata": doc.system_metadata,
                        "filename": doc.filename,
                        "content_type": doc.content_type,
                        "storage_info": doc.storage_info,
                        "storage_files": [
                            file.model_dump() if hasattr(file, "model_dump")
                            else (file.dict() if hasattr(file, "dict") else file)
                            for file in doc.storage_files
                        ] if doc.storage_files else []
                    }
                    success = await self.db.update_document(doc.external_id, updates, auth)
                    if not success:
                        raise Exception("Failed to update document metadata")
                else:
                    # For new documents, use store_document
                    success = await self.db.store_document(doc)
                    if not success:
                        raise Exception("Failed to store document metadata")
                break
            except Exception as e:
                attempt += 1
                error_msg = str(e)
                if "connection was closed" in error_msg or "ConnectionDoesNotExistError" in error_msg:
                    if attempt < max_retries:
                        logger.warning(f"Database connection error during document metadata storage (attempt {attempt}/{max_retries}): {error_msg}. Retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        # Increase delay for next retry (exponential backoff)
                        retry_delay *= 2
                    else:
                        logger.error(f"All database connection attempts failed after {max_retries} retries: {error_msg}")
                        raise Exception("Failed to store document metadata after multiple retries")
                else:
                    # For other exceptions, don't retry
                    logger.error(f"Error storing document metadata: {error_msg}")
                    raise
        
        logger.debug("Stored document metadata in database")
        logger.debug(f"Chunk IDs stored: {doc.chunk_ids}")
        return doc.chunk_ids

    async def _create_chunk_results(
        self, auth: AuthContext, chunks: List[DocumentChunk]
    ) -> List[ChunkResult]:
        """Create ChunkResult objects with document metadata."""
        results = []
        for chunk in chunks:
            # Get document metadata
            doc = await self.db.get_document(chunk.document_id, auth)
            if not doc:
                logger.warning(f"Document {chunk.document_id} not found")
                continue
            logger.debug(f"Retrieved metadata for document {chunk.document_id}")

            # Generate download URL if needed
            download_url = None
            if doc.storage_info:
                download_url = await self.storage.get_download_url(
                    doc.storage_info["bucket"], doc.storage_info["key"]
                )
                logger.debug(f"Generated download URL for document {chunk.document_id}")

            metadata = doc.metadata
            metadata["is_image"] = chunk.metadata.get("is_image", False)
            results.append(
                ChunkResult(
                    content=chunk.content,
                    score=chunk.score,
                    document_id=chunk.document_id,
                    chunk_number=chunk.chunk_number,
                    metadata=metadata,
                    content_type=doc.content_type,
                    filename=doc.filename,
                    download_url=download_url,
                )
            )

        logger.info(f"Created {len(results)} chunk results")
        return results

    async def _create_document_results(
        self, auth: AuthContext, chunks: List[ChunkResult]
    ) -> Dict[str, DocumentResult]:
        """Group chunks by document and create DocumentResult objects."""
        # Group chunks by document and get highest scoring chunk per doc
        doc_chunks: Dict[str, ChunkResult] = {}
        for chunk in chunks:
            if (
                chunk.document_id not in doc_chunks
                or chunk.score > doc_chunks[chunk.document_id].score
            ):
                doc_chunks[chunk.document_id] = chunk
        logger.info(f"Grouped chunks into {len(doc_chunks)} documents")
        logger.debug(f"Document chunks: {doc_chunks}")
        results = {}
        for doc_id, chunk in doc_chunks.items():
            # Get document metadata
            doc = await self.db.get_document(doc_id, auth)
            if not doc:
                logger.warning(f"Document {doc_id} not found")
                continue
            logger.info(f"Retrieved metadata for document {doc_id}")

            # Create DocumentContent based on content type
            if doc.content_type == "text/plain":
                content = DocumentContent(type="string", value=chunk.content, filename=None)
                logger.debug(f"Created text content for document {doc_id}")
            else:
                # Generate download URL for file types
                download_url = await self.storage.get_download_url(
                    doc.storage_info["bucket"], doc.storage_info["key"]
                )
                content = DocumentContent(type="url", value=download_url, filename=doc.filename)
                logger.debug(f"Created URL content for document {doc_id}")
            results[doc_id] = DocumentResult(
                score=chunk.score,
                document_id=doc_id,
                metadata=doc.metadata,
                content=content,
                additional_metadata=doc.additional_metadata,
            )

        logger.info(f"Created {len(results)} document results")
        return results

    async def create_cache(
        self,
        name: str,
        model: str,
        gguf_file: str,
        docs: List[Document | None],
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """Create a new cache with specified configuration.

        Args:
            name: Name of the cache to create
            model: Name of the model to use
            gguf_file: Name of the GGUF file to use
            filters: Optional metadata filters for documents to include
            docs: Optional list of specific document IDs to include
        """
        # Create cache metadata
        metadata = {
            "model": model,
            "model_file": gguf_file,
            "filters": filters,
            "docs": [doc.model_dump_json() for doc in docs],
            "storage_info": {
                "bucket": "caches",
                "key": f"{name}_state.pkl",
            },
        }

        # Store metadata in database
        success = await self.db.store_cache_metadata(name, metadata)
        if not success:
            logger.error(f"Failed to store cache metadata for cache {name}")
            return {"success": False, "message": f"Failed to store cache metadata for cache {name}"}

        # Create cache instance
        cache = self.cache_factory.create_new_cache(
            name=name, model=model, model_file=gguf_file, filters=filters, docs=docs
        )
        cache_bytes = cache.saveable_state
        base64_cache_bytes = base64.b64encode(cache_bytes).decode()
        bucket, key = await self.storage.upload_from_base64(
            base64_cache_bytes,
            key=metadata["storage_info"]["key"],
            bucket=metadata["storage_info"]["bucket"],
        )
        return {
            "success": True,
            "message": f"Cache created successfully, state stored in bucket `{bucket}` with key `{key}`",
        }

    async def load_cache(self, name: str) -> bool:
        """Load a cache into memory.

        Args:
            name: Name of the cache to load

        Returns:
            bool: Whether the cache exists and was loaded successfully
        """
        try:
            # Get cache metadata from database
            metadata = await self.db.get_cache_metadata(name)
            if not metadata:
                logger.error(f"No metadata found for cache {name}")
                return False

            # Get cache bytes from storage
            cache_bytes = await self.storage.download_file(
                metadata["storage_info"]["bucket"], "caches/" + metadata["storage_info"]["key"]
            )
            cache_bytes = cache_bytes.read()
            cache = self.cache_factory.load_cache_from_bytes(
                name=name, cache_bytes=cache_bytes, metadata=metadata
            )
            self.active_caches[name] = cache
            return {"success": True, "message": "Cache loaded successfully"}
        except Exception as e:
            logger.error(f"Failed to load cache {name}: {e}")
            # raise e
            return {"success": False, "message": f"Failed to load cache {name}: {e}"}

    async def update_document(
        self,
        document_id: str,
        auth: AuthContext,
        content: Optional[str] = None,
        file: Optional[UploadFile] = None,
        filename: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        rules: Optional[List] = None,
        update_strategy: str = "add",
        use_colpali: Optional[bool] = None,
    ) -> Optional[Document]:
        """
        Update a document with new content and/or metadata using the specified strategy.
        
        Args:
            document_id: ID of the document to update
            auth: Authentication context
            content: The new text content to add (either content or file must be provided)
            file: File to add (either content or file must be provided)
            filename: Optional new filename for the document
            metadata: Additional metadata to update
            rules: Optional list of rules to apply to the content
            update_strategy: Strategy for updating the document ('add' to append content)
            use_colpali: Whether to use multi-vector embedding
            
        Returns:
            Updated document if successful, None if failed
        """
        # Validate permissions and get document
        doc = await self._validate_update_access(document_id, auth)
        if not doc:
            return None
        
        # Get current content and determine update type
        current_content = doc.system_metadata.get("content", "")
        metadata_only_update = (content is None and file is None and metadata is not None)
        
        # Process content based on update type
        update_content = None
        file_content = None
        file_type = None
        file_content_base64 = None
        if content is not None:
            update_content = await self._process_text_update(content, doc, filename, metadata, rules)
        elif file is not None:
            update_content, file_content, file_type, file_content_base64 = await self._process_file_update(
                file, doc, metadata, rules
            )
            await self._update_storage_info(doc, file, file_content_base64)
        elif not metadata_only_update:
            logger.error("Neither content nor file provided for document update")
            return None
        
        # Apply content update strategy if we have new content
        if update_content:
            # Fix for initial file upload - if current_content is empty, just use the update_content
            # without trying to use the update strategy (since there's nothing to update)
            if not current_content:
                logger.info(f"No current content found, using only new content of length {len(update_content)}")
                updated_content = update_content
            else:
                updated_content = self._apply_update_strategy(current_content, update_content, update_strategy)
                logger.info(f"Applied update strategy '{update_strategy}': original length={len(current_content)}, new length={len(updated_content)}")
            
            # Always update the content in system_metadata
            doc.system_metadata["content"] = updated_content
            logger.info(f"Updated system_metadata['content'] with content of length {len(updated_content)}")
        else:
            updated_content = current_content
            logger.info(f"No content update - keeping current content of length {len(current_content)}")
        
        # Update metadata and version information
        self._update_metadata_and_version(doc, metadata, update_strategy, file)
        
        # For metadata-only updates, we don't need to re-process chunks
        if metadata_only_update:
            return await self._update_document_metadata_only(doc, auth)
        
        # Process content into chunks and generate embeddings
        chunks, chunk_objects = await self._process_chunks_and_embeddings(doc.external_id, updated_content)
        if not chunks:
            return None
        
        # Handle colpali (multi-vector) embeddings if needed
        chunk_objects_multivector = await self._process_colpali_embeddings(
            use_colpali, doc.external_id, chunks, file, file_type, file_content, file_content_base64
        )
        
        # Store everything - this will replace existing chunks with new ones
        await self._store_chunks_and_doc(
            chunk_objects, doc, use_colpali, chunk_objects_multivector, is_update=True, auth=auth
        )
        logger.info(f"Successfully updated document {doc.external_id}")
        
        return doc
        
    async def _validate_update_access(self, document_id: str, auth: AuthContext) -> Optional[Document]:
        """Validate user permissions and document access."""
        if "write" not in auth.permissions:
            logger.error(f"User {auth.entity_id} does not have write permission")
            raise PermissionError("User does not have write permission")
            
        # Check if document exists and user has write access
        doc = await self.db.get_document(document_id, auth)
        if not doc:
            logger.error(f"Document {document_id} not found or not accessible")
            return None
            
        if not await self.db.check_access(document_id, auth, "write"):
            logger.error(f"User {auth.entity_id} does not have write permission for document {document_id}")
            raise PermissionError(f"User does not have write permission for document {document_id}")
            
        return doc
        
    async def _process_text_update(
        self, 
        content: str, 
        doc: Document, 
        filename: Optional[str], 
        metadata: Optional[Dict[str, Any]], 
        rules: Optional[List]
    ) -> str:
        """Process text content updates."""
        update_content = content
        
        # Update filename if provided
        if filename:
            doc.filename = filename
        
        # Apply rules if provided for text content
        if rules:
            rule_metadata, modified_text = await self.rules_processor.process_rules(content, rules)
            # Update metadata with extracted metadata from rules
            if metadata is not None:
                metadata.update(rule_metadata)
            
            if modified_text:
                update_content = modified_text
                logger.info("Updated content with modified text from rules")
                
        return update_content
        
    async def _process_file_update(
        self,
        file: UploadFile,
        doc: Document,
        metadata: Optional[Dict[str, Any]],
        rules: Optional[List]
    ) -> tuple[str, bytes, Any, str]:
        """Process file content updates."""
        # Read file content
        file_content = await file.read()
        
        # Parse the file content
        additional_file_metadata, file_text = await self.parser.parse_file_to_text(
            file_content, file.filename
        )
        logger.info(f"Parsed file into text of length {len(file_text)}")
        
        # Apply rules if provided for file content
        if rules:
            rule_metadata, modified_text = await self.rules_processor.process_rules(file_text, rules)
            # Update metadata with extracted metadata from rules
            if metadata is not None:
                metadata.update(rule_metadata)
            
            if modified_text:
                file_text = modified_text
                logger.info("Updated file content with modified text from rules")
        
        # Add additional metadata from file if available
        if additional_file_metadata:
            if not doc.additional_metadata:
                doc.additional_metadata = {}
            doc.additional_metadata.update(additional_file_metadata)
        
        # Store file in storage if needed
        file_content_base64 = base64.b64encode(file_content).decode()
        
        # Store file in storage and update storage info
        await self._update_storage_info(doc, file, file_content_base64)
        
        # Store file type
        file_type = filetype.guess(file_content)
        if file_type:
            doc.content_type = file_type.mime
        else:
            # If filetype.guess failed, try to determine from filename
            import mimetypes
            guessed_type = mimetypes.guess_type(file.filename)[0]
            if guessed_type:
                doc.content_type = guessed_type
            else:
                # Default fallback
                doc.content_type = "text/plain" if file.filename.endswith('.txt') else "application/octet-stream"
        
        # Update filename
        doc.filename = file.filename
        
        return file_text, file_content, file_type, file_content_base64
        
    async def _update_storage_info(self, doc: Document, file: UploadFile, file_content_base64: str):
        """Update document storage information for file content."""
        # Initialize storage_files array if needed - using the passed doc object directly
        # No need to refetch from the database as we already have the full document state
        if not hasattr(doc, "storage_files") or not doc.storage_files:
            # Initialize empty list
            doc.storage_files = []
            
            # If storage_files is empty but we have storage_info, migrate legacy data
            if doc.storage_info and doc.storage_info.get("bucket") and doc.storage_info.get("key"):
                # Create StorageFileInfo from storage_info
                legacy_file_info = StorageFileInfo(
                    bucket=doc.storage_info.get("bucket", ""),
                    key=doc.storage_info.get("key", ""),
                    version=1,
                    filename=doc.filename,
                    content_type=doc.content_type,
                    timestamp=doc.system_metadata.get("updated_at", datetime.now(UTC))
                )
                doc.storage_files.append(legacy_file_info)
                logger.info(f"Migrated legacy storage_info to storage_files: {doc.storage_files}")
            
        # Upload the new file with a unique key including version number
        # The version is based on the current length of storage_files to ensure correct versioning
        version = len(doc.storage_files) + 1
        file_extension = os.path.splitext(file.filename)[1] if file.filename else ""
        storage_info = await self.storage.upload_from_base64(
            file_content_base64, f"{doc.external_id}_{version}{file_extension}", file.content_type
        )
        
        # Add the new file to storage_files
        new_file_info = StorageFileInfo(
            bucket=storage_info[0],
            key=storage_info[1],
            version=version,
            filename=file.filename,
            content_type=file.content_type,
            timestamp=datetime.now(UTC)
        )
        doc.storage_files.append(new_file_info)
        
        # Still update legacy storage_info with the latest file for backward compatibility
        doc.storage_info = {"bucket": storage_info[0], "key": storage_info[1]}
        logger.info(f"Stored file in bucket `{storage_info[0]}` with key `{storage_info[1]}`")
        
    def _apply_update_strategy(self, current_content: str, update_content: str, update_strategy: str) -> str:
        """Apply the update strategy to combine current and new content."""
        if update_strategy == "add":
            # Append the new content
            return current_content + "\n\n" + update_content
        else:
            # For now, just use 'add' as default strategy
            logger.warning(f"Unknown update strategy '{update_strategy}', defaulting to 'add'")
            return current_content + "\n\n" + update_content
        
    def _update_metadata_and_version(
        self, 
        doc: Document, 
        metadata: Optional[Dict[str, Any]], 
        update_strategy: str, 
        file: Optional[UploadFile]
    ):
        """Update document metadata and version tracking."""
        # Update metadata if provided - additive but replacing existing keys
        if metadata:
            doc.metadata.update(metadata)
            
        # Ensure external_id is preserved in metadata
        doc.metadata["external_id"] = doc.external_id
        
        # Increment version
        current_version = doc.system_metadata.get("version", 1)
        doc.system_metadata["version"] = current_version + 1
        doc.system_metadata["updated_at"] = datetime.now(UTC)
        
        # Track update history
        if "update_history" not in doc.system_metadata:
            doc.system_metadata["update_history"] = []
            
        update_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "version": current_version + 1,
            "strategy": update_strategy,
        }
        
        if file:
            update_entry["filename"] = file.filename
            
        if metadata:
            update_entry["metadata_updated"] = True
            
        doc.system_metadata["update_history"].append(update_entry)
        
        # Ensure storage_files models are properly typed as StorageFileInfo objects
        if hasattr(doc, "storage_files") and doc.storage_files:
            # Convert to StorageFileInfo objects if they're dicts or ensure they're properly serializable
            doc.storage_files = [
                StorageFileInfo(**file) if isinstance(file, dict) else
                (file if isinstance(file, StorageFileInfo) else 
                 StorageFileInfo(**file.model_dump()) if hasattr(file, "model_dump") else
                 StorageFileInfo(**file.dict()) if hasattr(file, "dict") else file)
                for file in doc.storage_files
            ]
        
    async def _update_document_metadata_only(self, doc: Document, auth: AuthContext) -> Optional[Document]:
        """Update document metadata without reprocessing chunks."""
        updates = {
            "metadata": doc.metadata,
            "system_metadata": doc.system_metadata,
            "filename": doc.filename,
            "storage_files": doc.storage_files if hasattr(doc, "storage_files") else None,
            "storage_info": doc.storage_info if hasattr(doc, "storage_info") else None,
        }
        # Remove None values
        updates = {k: v for k, v in updates.items() if v is not None}
        
        success = await self.db.update_document(doc.external_id, updates, auth)
        if not success:
            logger.error(f"Failed to update document {doc.external_id} metadata")
            return None
            
        logger.info(f"Successfully updated document metadata for {doc.external_id}")
        return doc
        
    async def _process_chunks_and_embeddings(self, doc_id: str, content: str) -> tuple[List[Chunk], List[DocumentChunk]]:
        """Process content into chunks and generate embeddings."""
        # Split content into chunks
        chunks = await self.parser.split_text(content)
        if not chunks:
            logger.error("No content chunks extracted after update")
            return None, None
            
        logger.info(f"Split updated text into {len(chunks)} chunks")
        
        # Generate embeddings for new chunks
        embeddings = await self.embedding_model.embed_for_ingestion(chunks)
        logger.info(f"Generated {len(embeddings)} embeddings")
        
        # Create new chunk objects
        chunk_objects = self._create_chunk_objects(doc_id, chunks, embeddings)
        logger.info(f"Created {len(chunk_objects)} chunk objects")
        
        return chunks, chunk_objects
        
    async def _process_colpali_embeddings(
        self,
        use_colpali: bool,
        doc_id: str,
        chunks: List[Chunk],
        file: Optional[UploadFile],
        file_type: Any,
        file_content: Optional[bytes],
        file_content_base64: Optional[str]
    ) -> List[DocumentChunk]:
        """Process colpali multi-vector embeddings if enabled."""
        chunk_objects_multivector = []
        
        if not (use_colpali and self.colpali_embedding_model and self.colpali_vector_store):
            return chunk_objects_multivector
            
        # For file updates, we need special handling for images and PDFs
        if file and file_type and (file_type.mime in IMAGE or file_type.mime == "application/pdf"):
            # Rewind the file and read it again if needed
            if hasattr(file, 'seek') and callable(file.seek) and not file_content:
                await file.seek(0)
                file_content = await file.read()
                file_content_base64 = base64.b64encode(file_content).decode()
            
            chunks_multivector = self._create_chunks_multivector(
                file_type, file_content_base64, file_content, chunks
            )
            logger.info(f"Created {len(chunks_multivector)} chunks for multivector embedding")
            colpali_embeddings = await self.colpali_embedding_model.embed_for_ingestion(chunks_multivector)
            logger.info(f"Generated {len(colpali_embeddings)} embeddings for multivector embedding")
            chunk_objects_multivector = self._create_chunk_objects(
                doc_id, chunks_multivector, colpali_embeddings
            )
        else:
            # For text updates or non-image/PDF files
            embeddings_multivector = await self.colpali_embedding_model.embed_for_ingestion(chunks)
            logger.info(f"Generated {len(embeddings_multivector)} embeddings for multivector embedding")
            chunk_objects_multivector = self._create_chunk_objects(
                doc_id, chunks, embeddings_multivector
            )
            
        logger.info(f"Created {len(chunk_objects_multivector)} chunk objects for multivector embedding")
        return chunk_objects_multivector

    async def create_graph(
        self,
        name: str,
        auth: AuthContext,
        filters: Optional[Dict[str, Any]] = None,
        documents: Optional[List[str]] = None,
        prompt_overrides: Optional[GraphPromptOverrides] = None,
        system_filters: Optional[Dict[str, Any]] = None,
    ) -> Graph:
        """Create a graph from documents.

        This function processes documents matching filters or specific document IDs,
        extracts entities and relationships from document chunks, and saves them as a graph.

        Args:
            name: Name of the graph to create
            auth: Authentication context
            filters: Optional metadata filters to determine which documents to include
            documents: Optional list of specific document IDs to include
            prompt_overrides: Optional customizations for entity extraction and resolution prompts
            system_filters: Optional system filters like folder_name and end_user_id for scoping

        Returns:
            Graph: The created graph
        """
        # Delegate to the GraphService
        return await self.graph_service.create_graph(
            name=name,
            auth=auth,
            document_service=self,
            filters=filters,
            documents=documents,
            prompt_overrides=prompt_overrides,
            system_filters=system_filters,
        )
        
    async def update_graph(
        self,
        name: str,
        auth: AuthContext,
        additional_filters: Optional[Dict[str, Any]] = None,
        additional_documents: Optional[List[str]] = None,
        prompt_overrides: Optional[GraphPromptOverrides] = None,
        system_filters: Optional[Dict[str, Any]] = None,
    ) -> Graph:
        """Update an existing graph with new documents.
        
        This function processes additional documents matching the original or new filters,
        extracts entities and relationships, and updates the graph with new information.
        
        Args:
            name: Name of the graph to update
            auth: Authentication context
            additional_filters: Optional additional metadata filters to determine which new documents to include
            additional_documents: Optional list of additional document IDs to include
            prompt_overrides: Optional customizations for entity extraction and resolution prompts
            system_filters: Optional system filters like folder_name and end_user_id for scoping
            
        Returns:
            Graph: The updated graph
        """
        # Delegate to the GraphService
        return await self.graph_service.update_graph(
            name=name,
            auth=auth,
            document_service=self,
            additional_filters=additional_filters,
            additional_documents=additional_documents,
            prompt_overrides=prompt_overrides,
            system_filters=system_filters,
        )

    async def delete_document(self, document_id: str, auth: AuthContext) -> bool:
        """
        Delete a document and all its associated data.

        This method:
        1. Checks if the user has write access to the document
        2. Gets the document to retrieve its chunk IDs
        3. Deletes the document from the database
        4. Deletes all associated chunks from the vector store (if possible)
        5. Deletes the original file from storage if present

        Args:
            document_id: ID of the document to delete
            auth: Authentication context

        Returns:
            bool: True if deletion was successful, False otherwise

        Raises:
            PermissionError: If the user doesn't have write access
        """
        # First get the document to retrieve its chunk IDs
        document = await self.db.get_document(document_id, auth)

        if not document:
            logger.error(f"Document {document_id} not found")
            return False

        # Verify write access - the database layer also checks this, but we check here too
        # to avoid unnecessary operations if the user doesn't have permission
        if not await self.db.check_access(document_id, auth, "write"):
            logger.error(f"User {auth.entity_id} doesn't have write access to document {document_id}")
            raise PermissionError(f"User doesn't have write access to document {document_id}")

        # Delete document from database
        db_success = await self.db.delete_document(document_id, auth)
        if not db_success:
            logger.error(f"Failed to delete document {document_id} from database")
            return False
            
        logger.info(f"Deleted document {document_id} from database")
        
        # Try to delete chunks from vector store if they exist
        if hasattr(document, 'chunk_ids') and document.chunk_ids:
            try:
                # Try to delete chunks by document ID
                # Note: Some vector stores may not implement this method
                if hasattr(self.vector_store, 'delete_chunks_by_document_id'):
                    await self.vector_store.delete_chunks_by_document_id(document_id)
                    logger.info(f"Deleted chunks for document {document_id} from vector store")
                else:
                    logger.warning(f"Vector store does not support deleting chunks by document ID")
                
                # Try to delete from colpali vector store as well
                if self.colpali_vector_store and hasattr(self.colpali_vector_store, 'delete_chunks_by_document_id'):
                    await self.colpali_vector_store.delete_chunks_by_document_id(document_id)
                    logger.info(f"Deleted chunks for document {document_id} from colpali vector store")
            except Exception as e:
                logger.error(f"Error deleting chunks for document {document_id}: {e}")
                # We continue even if chunk deletion fails - don't block document deletion
        
        # Delete file from storage if it exists
        if hasattr(document, 'storage_info') and document.storage_info:
            try:
                bucket = document.storage_info.get("bucket")
                key = document.storage_info.get("key")
                if bucket and key:
                    # Check if the storage provider supports deletion
                    if hasattr(self.storage, 'delete_file'):
                        await self.storage.delete_file(bucket, key)
                        logger.info(f"Deleted file for document {document_id} from storage (bucket: {bucket}, key: {key})")
                    else:
                        logger.warning(f"Storage provider does not support file deletion")
                
                # Also handle the case of multiple file versions in storage_files
                if hasattr(document, 'storage_files') and document.storage_files:
                    for file_info in document.storage_files:
                        bucket = file_info.get("bucket")
                        key = file_info.get("key")
                        if bucket and key and hasattr(self.storage, 'delete_file'):
                            await self.storage.delete_file(bucket, key)
                            logger.info(f"Deleted file version for document {document_id} from storage (bucket: {bucket}, key: {key})")
            except Exception as e:
                logger.error(f"Error deleting file for document {document_id}: {e}")
                # We continue even if file deletion fails - don't block document deletion
        
        logger.info(f"Successfully deleted document {document_id} and all associated data")
        return True
    
    def close(self):
        """Close all resources."""
        # Close any active caches
        self.active_caches.clear()
