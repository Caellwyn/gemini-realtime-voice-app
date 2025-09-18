"""
Tool response builder for handling Gemini API function calls.
Consolidates response creation patterns and reduces code duplication.
"""

import json
import time
from typing import Dict, Any, List, Optional
import websockets

from logging_utils import log_tool_call


class ToolCall:
    """Represents a single tool call with metadata."""
    
    def __init__(self, name: str, args: Dict[str, Any], call_id: str):
        self.name = name
        self.args = args or {}
        self.call_id = call_id
        self.start_time = time.time()
    
    def get_execution_time(self) -> float:
        """Get time elapsed since tool call started."""
        return time.time() - self.start_time


class ToolResponse:
    """Represents a tool response with structured data."""
    
    def __init__(self, tool_call: ToolCall, result: Any, errors: Optional[List[str]] = None):
        self.tool_call = tool_call
        self.result = result
        self.errors = errors or []
        self.success = len(self.errors) == 0
    
    def to_function_response(self) -> Dict[str, Any]:
        """Convert to Gemini API function response format."""
        response_data = {"result": self.result}
        if self.errors:
            response_data["errors"] = self.errors
        
        return {
            "name": self.tool_call.name,
            "response": response_data,
            "id": self.tool_call.call_id
        }
    
    def log_execution(self, session_id: str):
        """Log the tool call execution."""
        log_tool_call(
            session_id=session_id,
            tool_name=self.tool_call.name,
            request=self.tool_call.args,
            response=self.result,
            started_ts=self.tool_call.start_time
        )


class ClientNotification:
    """Represents a notification to send to the client WebSocket."""
    
    def __init__(self, message_type: str, data: Any):
        self.message_type = message_type
        self.data = data
    
    def to_json(self) -> str:
        """Convert to JSON string for WebSocket transmission."""
        return json.dumps({self.message_type: self.data})
    
    async def send_to_client(self, client_websocket: websockets.ServerProtocol) -> bool:
        """Send notification to client WebSocket."""
        try:
            await client_websocket.send(self.to_json())
            return True
        except Exception:
            # Failed to send client notification
            return False


class ToolResponseBuilder:
    """Builds tool responses with client notifications and logging."""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.responses: List[ToolResponse] = []
        self.notifications: List[ClientNotification] = []
    
    def add_state_response(self, tool_call: ToolCall, state_snapshot: Dict[str, Any], 
                          notification_type: str) -> 'ToolResponseBuilder':
        """
        Add a state query response (get_profile_state, get_form_state).
        
        Args:
            tool_call: The tool call object
            state_snapshot: Current state data
            notification_type: Type of client notification to send
        """
        response = ToolResponse(tool_call, state_snapshot)
        self.responses.append(response)
        
        notification = ClientNotification(notification_type, state_snapshot)
        self.notifications.append(notification)
        
        return self
    
    def add_pdf_form_response(self, tool_call: ToolCall, 
                             update_result: Dict[str, Any]) -> 'ToolResponseBuilder':
        """
        Add a PDF form update response.
        
        Args:
            tool_call: The tool call object
            update_result: Result from form_manager.update_fields()
        """
        response = ToolResponse(tool_call, update_result)
        self.responses.append(response)
        
        # Send detailed client notification if there were applied updates
        if update_result.get("applied"):
            notification_data = {
                "updated": update_result.get("applied"),
                "remaining": update_result.get("remaining_empty_count"),
                "unknown": update_result.get("unknown_fields"),
                "catalog_hash": update_result.get("catalog_hash")
            }
            notification = ClientNotification("form_tool_response", notification_data)
            self.notifications.append(notification)
        
        # Send completion notification if form is complete
        if update_result.get("complete"):
            completion_notification = ClientNotification("form_complete", True)
            self.notifications.append(completion_notification)
        
        return self
    
    def get_function_responses(self) -> List[Dict[str, Any]]:
        """Get all function responses for Gemini API."""
        return [response.to_function_response() for response in self.responses]
    
    async def send_client_notifications(self, client_websocket: websockets.ServerProtocol) -> int:
        """
        Send all client notifications.
        
        Returns:
            Number of notifications sent successfully
        """
        sent_count = 0
        for notification in self.notifications:
            if await notification.send_to_client(client_websocket):
                sent_count += 1
        return sent_count
    
    def log_all_executions(self):
        """Log all tool call executions."""
        for response in self.responses:
            response.log_execution(self.session_id)
    
    async def finalize(self, client_websocket: websockets.ServerProtocol) -> List[Dict[str, Any]]:
        """
        Complete the response building process.
        
        Sends all client notifications, logs executions, and returns function responses.
        
        Returns:
            List of function responses for Gemini API
        """
        await self.send_client_notifications(client_websocket)
        self.log_all_executions()
        return self.get_function_responses()


class ToolCallHandler:
    """High-level handler for processing tool calls using the response builder."""
    
    @staticmethod
    async def handle_pdf_form_tools(tool_call: ToolCall, form_manager, 
                                  client_websocket: websockets.ServerProtocol, 
                                  pdf_sync) -> List[Dict[str, Any]]:
        """Handle PDF form tool calls."""
        builder = ToolResponseBuilder(pdf_sync.form_id)
        
        if tool_call.name == "get_form_state":
            state_snapshot = form_manager.get_state_snapshot()
            builder.add_state_response(tool_call, state_snapshot, "form_state")
            
        elif tool_call.name == "update_pdf_fields":
            update_result = form_manager.update_fields(tool_call.args)
            builder.add_pdf_form_response(tool_call, update_result)
            
            # Handle PDF sync if there were updates
            if update_result.get("applied"):
                await pdf_sync.sync_updates(update_result.get("applied", {}))
                await pdf_sync.schedule_full_sync(form_manager)
                # Also include a current state snapshot for UI reconciliation
                try:
                    state_snapshot = form_manager.get_state_snapshot()
                    builder.add_state_response(tool_call, state_snapshot, "form_state")
                except Exception:
                    pass
        
        return await builder.finalize(client_websocket)


# Convenience function for backward compatibility
async def build_tool_response(session_id: str, tool_name: str, tool_args: Dict[str, Any], 
                            call_id: str, result_data: Any, 
                            client_websocket: websockets.ServerProtocol,
                            notification_type: Optional[str] = None,
                            notification_data: Optional[Any] = None) -> Dict[str, Any]:
    """
    Build a single tool response with optional client notification.
    
    This is a convenience function for simple tool response patterns.
    """
    tool_call = ToolCall(tool_name, tool_args, call_id)
    response = ToolResponse(tool_call, result_data)
    
    builder = ToolResponseBuilder(session_id)
    builder.responses.append(response)
    
    if notification_type and notification_data is not None:
        notification = ClientNotification(notification_type, notification_data)
        builder.notifications.append(notification)
    
    responses = await builder.finalize(client_websocket)
    return responses[0] if responses else {}