# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import itertools
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional, List 
from collections import deque

from vllm.logger import init_logger
from vllm.sequence import Logprob, PromptLogprobs, SampleLogprobs
from vllm.transformers_utils.detokenizer_utils import (
    AnyTokenizer, convert_ids_list_to_tokens)
from vllm.v1.engine import EngineCoreOutput, EngineCoreRequest
from vllm.v1.outputs import LogprobsLists, LogprobsTensors

logger = init_logger(__name__)

NONES = itertools.repeat(None)


@dataclass
class LogprobsProcessor:

    # Fields for confidence-based early stopping
    conf_grouped: float
    conf_list: Optional[List[float]]
    conf_group_list: Optional[deque]
    conf_group_size: int
    conf_threshold: Optional[float]
    
    # Tokenizer for this request,
    # None if detokenization is disabled.
    tokenizer: Optional[AnyTokenizer]

    # Logprobs for this request
    logprobs: Optional[SampleLogprobs]
    prompt_logprobs: Optional[PromptLogprobs]
    cumulative_logprob: Optional[float]
    num_logprobs: Optional[int]
    num_prompt_logprobs: Optional[int]

    @classmethod
    def from_new_request(
        cls,
        tokenizer: Optional[AnyTokenizer],
        request: EngineCoreRequest,
    ) -> "LogprobsProcessor":
        assert request.sampling_params is not None
        num_logprobs = request.sampling_params.logprobs
        num_prompt_logprobs = request.sampling_params.prompt_logprobs
        
        if (hasattr(request.sampling_params, "extra_args")
                and request.sampling_params.extra_args is not None and
                request.sampling_params.extra_args.get("enable_conf", False)):
            conf_group_size = request.sampling_params.extra_args.get("window_size", 2048)
            conf_threshold = request.sampling_params.extra_args.get("threshold", 17)
            conf_grouped = 0.0
            conf_group_list = deque(maxlen=conf_group_size)
            conf_list = []
        else:
            conf_group_size = -1
            conf_threshold = None
            conf_grouped = 0.0
            conf_group_list = None
            conf_list = None
            
        return cls(
            conf_group_size=conf_group_size,
            conf_grouped=conf_grouped,
            conf_list=conf_list,
            conf_group_list=conf_group_list,
            conf_threshold=conf_threshold,
            tokenizer=tokenizer,
            cumulative_logprob=(None if num_logprobs is None else 0.),
            logprobs=(None if num_logprobs is None else []),
            # NOTE: logprob of first prompt token is None.
            prompt_logprobs=(None if num_prompt_logprobs is None else [None]),
            num_prompt_logprobs=num_prompt_logprobs,
            num_logprobs=num_logprobs,
        )

    def _update_sample_logprobs(self, logprobs_lists: LogprobsLists) -> None:
        """Update with sample logprobs from EngineCore.

        Outer lists are only of len > 1 if EngineCore made
        >1 tokens in prior step (e.g. in spec decoding).

        Args:
          logprobs_lists: the lists of logprob tokens, logprobs, and ranks.

        """

        assert self.num_logprobs is not None
        assert self.logprobs is not None
        assert self.cumulative_logprob is not None

        token_ids_lst, logprobs_lst, ranks_lst = logprobs_lists

        for rank, logprobs, token_ids in zip(ranks_lst, logprobs_lst,
                                             token_ids_lst):

            # Detokenize (non-incrementally).
            decoded_tokens = NONES if self.tokenizer is None else (
                convert_ids_list_to_tokens(self.tokenizer, token_ids))

            # Sampler puts the sampled logprob in first.
            sampled_token_logprob = logprobs[0]
            self.cumulative_logprob += sampled_token_logprob

            # Update with the Logprob dictionary for this pos.
            self.logprobs.append(
                self._make_logprob_dict(
                    logprobs,
                    token_ids,
                    decoded_tokens,
                    rank,
                    self.num_logprobs,
                ))
            
            if self.conf_list is not None:
                if len(logprobs) > 1:
                    new_conf = -sum(logprobs[1:]) / len(logprobs[1:])
                else:
                    new_conf = 0.0

                self.conf_list.append(new_conf)

                if len(self.conf_group_list) < self.conf_group_size:
                    self.conf_group_list.append(new_conf)
                    self.conf_grouped += new_conf
                else:
                    self.conf_grouped -= self.conf_group_list.popleft()
                    self.conf_group_list.append(new_conf)
                    self.conf_grouped += new_conf

    def _update_prompt_logprobs(
        self,
        prompt_logprobs_tensors: LogprobsTensors,
    ) -> None:
        """Update with prompt logprobs from EngineCore.

        Args:
          prompt_logprobs_tensors: tuple containing the prompt logprobs
                                   tensors.

        """

        # Prompt logprobs are enabled.
        assert self.num_prompt_logprobs is not None
        assert self.prompt_logprobs is not None

        token_ids, logprobs, ranks = prompt_logprobs_tensors

        # Detokenize non-incrementally.
        # Output is flat: [num_tok, num_lps] -> [num_tok * num_lps]
        decoded_tokens = None if self.tokenizer is None else (
            convert_ids_list_to_tokens(self.tokenizer,
                                       token_ids.flatten().tolist()))

        # Recover shapes.
        num_prompt_tokens, num_logprobs = logprobs.shape

        # Pythonize the torch tensors.
        prompt_token_ranks = ranks.tolist()
        prompt_logprobs = logprobs.tolist()
        token_ids = token_ids.tolist()

        # Make Logprob for each position.
        for pos in range(num_prompt_tokens):
            # Handle flattening.
            offset = pos * num_logprobs
            offset_end = offset + num_logprobs
            decoded_tokens_for_pos = NONES \
            if decoded_tokens is None else decoded_tokens[offset:offset_end]

            # Update with the Logprob dictionary for this pos.
            self.prompt_logprobs.append(
                self._make_logprob_dict(prompt_logprobs[pos], token_ids[pos],
                                        decoded_tokens_for_pos,
                                        prompt_token_ranks[pos],
                                        self.num_prompt_logprobs))

    def pop_prompt_logprobs(self) -> Optional[PromptLogprobs]:
        """Pop and return all request prompt logprobs

        The logprobs processor aggregates prompt chunk logprobs
        over one or more prefill chunks. This method returns
        all prompt logprobs at once and then forgets them.
        Ensures correct RequestOutputKind.DELTA semantics
        wherein all prompt logprobs are returned at once at
        the end of prefill.

        Returns:
          None if prompt logprobs are disabled for this request.
          List of all prompt logprobs, otherwise.
        """
        plp = self.prompt_logprobs
        if plp:
            self.prompt_logprobs = []
        return plp

    @staticmethod
    def _make_logprob_dict(
        logprobs: list[float],
        logprob_token_ids: list[int],
        decoded_tokens: Iterable[Optional[str]],
        rank: int,
        num_logprobs: int,
    ) -> dict[int, Logprob]:
        """Make a Logprob dictionary for a position.

        Args:
          logprobs: list of log probabilities
          logprob_token_ids: list of top token ids
          decoded_tokens: list of decoded top tokens
          rank: rank of the sampled token
          num_logprobs: number of logprobs requested
            by the user (in addition to sampled logprob)

        Returns:
          dict[token id, Logprob]
        """
        if num_logprobs == -1:
            num_logprobs = len(logprobs)
        # We do not need a special case for the sampled token
        # being in the topk, since inserting duplicated data
        # into a dictionary twice is the same as doing it once.
        topk_ranks = range(1, num_logprobs + 1)
        ranks = itertools.chain((rank, ), topk_ranks)

        return {
            token_id: Logprob(
                logprob=logprob,
                rank=rank,
                decoded_token=token,
            )
            for token_id, logprob, rank, token in zip(
                logprob_token_ids, logprobs, ranks, decoded_tokens)
        }

    def update_from_output(self, output: EngineCoreOutput) -> None:
        if output.new_logprobs is not None:
            self._update_sample_logprobs(output.new_logprobs)
        if output.new_prompt_logprobs_tensors is not None:
            self._update_prompt_logprobs(output.new_prompt_logprobs_tensors)
            
    def check_conf_stop(self) -> bool:
        """Return True if the confidence window triggers early stopping."""
        if self.conf_group_list is None or len(self.conf_group_list) == 0:
            return False
        # Require a full window; trigger when the moving average is below threshold.
        return (len(self.conf_group_list) >= self.conf_group_size
                and self.conf_grouped / len(self.conf_group_list) < self.conf_threshold)