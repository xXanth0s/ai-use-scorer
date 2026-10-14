import { defineInstructions } from 'eve/instructions';
import { z } from 'zod';
import { RUBRIC, reportSchema } from './lib/contract';
export default defineInstructions({content:`You are the assessment agent for how do you ai. ${RUBRIC}\nReturn one JSON object, without markdown fences, matching this schema: ${JSON.stringify(z.toJSONSchema(reportSchema))}`});
