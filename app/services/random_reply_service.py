async def execute_random_reply(macro_id: str) -> dict:
    """Execute a random reply macro.

    For each target chat, fetch recent messages, pick a random one from a unique user
    who hasn't been replied to before, and send the macro's message as a reply.
    """
    lock = _get_macro_lock(macro_id)
    async with lock:
        return await _execute_random_reply_impl(macro_id)


async def _execute_random_reply_impl(macro_id: str) -> dict:
    """Internal implementation  callers must hold the per-macro lock."""
    async with async_session_maker() as db:
        macro = await macro_crud.get_macro(db, macro_id)
        if macro is None or not macro.is_active:
            return {"status": "skipped", "reason": "not_found_or_inactive"}

        account = await account_crud.get_account(db, macro.account_id)
        if account is None:
            return {"status": "failed", "reason": "account_not_found"}

        target_chats = _parse_target_chats(macro.target_chats)

        used_set: set[tuple[str, str]] = set()  # 중복 체크 없이 매시간 무작위 발송

    try:
        client = await get_authorized_client(account)
    except AccountNotAuthenticatedError:
        return {"status": "failed", "reason": "not_authenticated"}

    if not client.is_connected():
        logger.warning("random_reply_client_disconnected", macro_id=macro_id, account_id=account.id)
        return {"status": "failed", "reason": "client_disconnected"}

    # No manually-picked targets (the simplified on/off toggle never sets any) 
    # resolve to every group/channel this account is currently a member of.
    if not target_chats:
        try:
            target_chats = [
                str(d.entity.id)
                async for d in client.iter_dialogs()
                if d.is_group or d.is_channel
            ]
        except Exception as exc:
            logger.warning("random_reply_dialog_list_failed", macro_id=macro_id, error=str(exc))
            return {"status": "failed", "reason": "dialog_list_failed"}
        if not target_chats:
            return {"status": "skipped", "reason": "no_groups"}

    self_id = None
    try:
        self_user = await client.get_me()
        self_id = str(getattr(self_user, "id", "")) if self_user else None
    except Exception as exc:
        logger.debug("random_reply_get_me_failed", macro_id=macro_id, error=str(exc))

    results = []
    async with async_session_maker() as db:
        macro = await macro_crud.get_macro(db, macro_id)
        if macro is None:
            return {"status": "failed", "reason": "macro_deleted"}

        # NEW: Create a single Broadcast entry representing this entire random reply execution
        broadcast_entry_created = False
        broadcast_id = None
        broadcast_finalized_status = False # NEW: Flag to track if the broadcast status was finalized successfully
        try:
            broadcast_data = BroadcastCreate(
                account_id=macro.account_id,
                message=macro.message_content,
                recipients=target_chats, # Use the full list of target chats
                # status will default to 'pending' in create_broadcast
            )
            broadcast_entry = await broadcast_crud.create_broadcast(
                db, # Use the same session for consistency in this scope
                data=broadcast_data,
                media_path=macro.media_path,
                scheduled_at=None # Not a scheduled send
            )
            broadcast_id = broadcast_entry.id
            broadcast_entry_created = True
            logger.info("random_reply_broadcast_entry_created", broadcast_id=broadcast_id, macro_id=macro_id)
        except Exception as e:
            logger.error("random_reply_broadcast_creation_failed", macro_id=macro_id, error=str(e))
            # Do not fail the entire random reply execution if broadcast log creation fails
            # Proceed with the actual sending logic

        # NEW: Wrap the main sending logic in try...finally
        try:
            for chat_id in target_chats:
                try:
                    cleaned = chat_id.lstrip("-")
                    target = int(chat_id) if cleaned.isdigit() else chat_id
                    messages = await client.get_messages(target, limit=20)
                except Exception as exc:
                    logger.warning("random_reply: failed to fetch messages for %s: %s", chat_id, exc)
                    results.append({"chat_id": chat_id, "user_id": None, "status": "failed", "error": str(exc)})
                    continue

                candidates = await _build_candidate_pool(
                    messages,
                    chat_id=chat_id,
                    used_pairs=used_set,
                    self_id=self_id,
                )

                if not candidates:
                    logger.info(
                        "random_reply_no_candidates",
                        macro_id=macro_id,
                        chat_id=chat_id,
                        reason="filtered_or_used",
                        used_count=len(used_set),
                    )
                    results.append({"chat_id": chat_id, "user_id": None, "status": "skipped", "reason": "no_candidates"})
                    continue

                chosen_uid, chosen_msg = random.choice(candidates)

                request = DeliveryRequest(
                    account_id=macro.account_id,
                    recipients=[chat_id],
                    message=macro.message_content,
                    media_path=macro.media_path,
                    source="random_reply",
                    source_id=macro.id,
                    reply_to_map={chat_id: chosen_msg.id},
                    inter_message_delay=0.2,  # 기본 1.0초 → 0.2초로 단축 (실시간 속도, 계정 차단 위험 감수)
                )

                try:
                    delivery_results = await deliver_message(request, client=client)
                except Exception as exc:
                    logger.error("random_reply_delivery_failed", macro_id=macro_id, chat_id=chat_id, error=str(exc))
                    results.append({"chat_id": chat_id, "user_id": chosen_uid, "status": "failed", "error": str(exc)})
                    continue

                for dr in delivery_results:
                    is_success = dr.status == DeliveryStatus.SUCCESS
                    await macro_crud.create_log(
                        db,
                        macro_id=macro.id,
                        account_id=macro.account_id,
                        target_chat_id=chat_id,
                        replied_user_id=chosen_uid,
                        replied_msg_id=chosen_msg.id,
                        message_sent=macro.message_content,
                        status="success" if is_success else "failed",
                        error_message=dr.error_message if not is_success else None,
                    )
                    if is_success:
                        await macro_crud.add_used_target(db, macro, chat_id, chosen_uid)
                        used_set.add((chat_id, chosen_uid))
                    else:
                        logger.warning(
                            "random_reply_delivery_failed",
                            macro_id=macro_id,
                            chat_id=chat_id,
                            user_id=chosen_uid,
                            error=dr.error_message,
                        )
                    results.append({
                        "chat_id": chat_id,
                        "user_id": chosen_uid,
                        "status": "success" if is_success else "failed",
                    })

            await macro_crud.mark_macro_sent(db, macro)

            # NEW: After the loop, update the single Broadcast entry based on overall results
            if broadcast_entry_created and broadcast_id:
                try:
                    # Calculate overall status and error message
                    success_count = sum(1 for r in results if r["status"] == "success")
                    failed_count = sum(1 for r in results if r["status"] == "failed")
                    skipped_count = sum(1 for r in results if r["status"] == "skipped")

                    overall_status = "failed"  # Assume failure initially
                    overall_error_message = None
                    overall_sent_at = None

                    if success_count > 0:
                        # If any succeeded, consider the overall action as sent
                        overall_status = "sent"
                        overall_sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    elif failed_count > 0:
                        # If no successes, but some failures, it's still considered a 'failed' attempt
                        overall_status = "failed"
                        overall_error_message = f"Random reply failed for {failed_count} out of {len(results)} targets."
                    elif skipped_count > 0:
                        # If all were skipped (e.g., no candidates), it's a form of failure or a no-op
                        overall_status = "failed" # Or possibly 'pending' depending on UI interpretation, but 'failed' indicates no actual sends.
                        overall_error_message = f"Random reply skipped all {skipped_count} targets (e.g., no candidates)."

                    # Update the broadcast entry in the same session or a new one if needed
                    # To update, we need to fetch the broadcast object again in the current session
                    async with async_session_maker() as update_db_session:
                        broadcast_to_update = await update_db_session.get(Broadcast, broadcast_id)
                        if broadcast_to_update:
                            broadcast_to_update.status = overall_status
                            broadcast_to_update.sent_at = overall_sent_at
                            broadcast_to_update.error_message = overall_error_message
                            await update_db_session.commit()
                            logger.info("random_reply_broadcast_entry_updated", broadcast_id=broadcast_id, status=overall_status, macro_id=macro_id)
                            # NEW: Set the flag to indicate the status was successfully finalized
                            broadcast_finalized_status = True
                        else:
                            logger.warning("random_reply_broadcast_entry_not_found_for_update", broadcast_id=broadcast_id, macro_id=macro_id)

                except Exception as e:
                    logger.error("random_reply_broadcast_update_failed", broadcast_id=broadcast_id, macro_id=macro_id, error=str(e))
                    # Still return the original result of the random reply execution
                    # broadcast_finalized_status remains False

        finally:
            # NEW: This block runs whether the try block succeeded or failed
            # If the broadcast was created but its status was NOT finalized (due to an exception in the main logic
            # or a failure during the update commit), mark it as failed.
            if broadcast_entry_created and broadcast_id and not broadcast_finalized_status:
                try:
                    async with async_session_maker() as finalize_db_session:
                        broadcast_to_finalize = await finalize_db_session.get(Broadcast, broadcast_id)
                        if broadcast_to_finalize:
                            # Check if status is still 'pending', meaning it wasn't updated by the main logic
                            if broadcast_to_finalize.status == "pending":
                                broadcast_to_finalize.status = "failed"
                                broadcast_to_finalize.error_message = "Random reply execution failed unexpectedly before completion or status update commit failed."
                                broadcast_to_finalize.sent_at = datetime.now(timezone.utc).replace(tzinfo=None) # Set sent_at for consistency on failure too, or leave null?
                                await finalize_db_session.commit()
                                logger.info("random_reply_broadcast_entry_finalized_as_failed", broadcast_id=broadcast_id, macro_id=macro_id)
                        else:
                            logger.warning("random_reply_broadcast_entry_not_found_for_finalization", broadcast_id=broadcast_id, macro_id=macro_id)
                except Exception as e:
                    logger.error("random_reply_broadcast_finalization_failed", broadcast_id=broadcast_id, macro_id=macro_id, error=str(e))
                    # Do not re-raise or affect the original execution flow


    failed_count = sum(1 for r in results if r["status"] == "failed")
    if failed_count:
        logger.warning("random_reply_partial_failure", macro_id=macro_id, failed=failed_count, total=len(results))

    return {"status": "completed", "results": results}