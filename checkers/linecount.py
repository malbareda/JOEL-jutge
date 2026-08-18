from re import split as resplit
from typing import Union

from dmoj.result import CheckerResult
from dmoj.utils.unicode import utf8bytes, utf16bytes

verdict = u"\u2717\u2713"


def check(
    process_output: bytes, judge_output: bytes, point_value: float, feedback: bool = True, **kwargs
) -> Union[CheckerResult, bool]:

    #Maniobra Anti-Izan
    print(((len(process_output))))
    if(len(process_output)>10000000):
         return CheckerResult(
            False, 0, "PARA JA AMB ELS BUCLES INFINTS. SÉ QUI ETS.", None )
    
    #print(process_output.decode('utf-16'))
    jutgeout = judge_output.decode("utf-8")
    #print("el jutge",jutgeout)

    judge_input = list(filter(None, resplit(b'[\r\n]', utf8bytes(kwargs["judge_input"]))))
    process_lines = list(filter(None, resplit(b'[\r\n]', utf8bytes(process_output))))
    process_lines16 = list(filter(None, resplit(b'[\r\n]', utf16bytes(process_output))))
    judge_lines = list(filter(None, resplit(b'[\r\n]', utf8bytes(judge_output))))
    #print("el de 16",process_lines16)
    print("el de 8",process_lines)
    senseEspaisJ = b''.join(judge_lines).replace(b' ',b'').replace(b'[\r\n]',b'')
    senseEspaisP = b''.join(process_lines).replace(b' ',b'').replace(b'[\r\n]',b'')

    #print(senseEspaisP)
    #print(senseEspaisJ)
    if(senseEspaisJ==senseEspaisP):
        print("sembla que es tema d'espais")
    print("hey, nomes volia dir "+str(kwargs["cosa"]))

    lowerJ = (senseEspaisJ.lower())
    lowerP = (senseEspaisP.lower())


    enjin = [x.decode('utf-8') for x in judge_input]
    strinput = '\n'.join(map(str, enjin))

    processLines = len(process_lines)
    judgeLines = len(judge_lines)


    if len(process_lines) > len(judge_lines):
        while len(process_lines) > len(judge_lines):
            ch = '\u2717'
            ch = ch.encode('utf-8')
            judge_lines.append(ch)
    elif len(process_lines) < len(judge_lines):
        while len(process_lines) < len(judge_lines):
            ch = '\u2717'
            ch = ch.encode('utf-8')
            process_lines.append(ch)

    if not judge_lines:
        return True

    cases = [verdict[0]] * len(judge_lines)
    count = 0
    wa = 0
    wronganswers = ""
    for i, (process_line, judge_line) in enumerate(zip(process_lines, judge_lines)):
        process_line = process_line.strip()
        judge_line = judge_line.strip()
        if process_line.strip() == judge_line.strip():
            cases[i] = verdict[1]
            count += 1
        else:
            if wa<5:
                tmpl = str(i)+"\u2720"+str(judge_line.strip().decode('utf-8'))+"\u2720"+str(process_line.strip().decode('utf-8'))+"\u2721"
                wronganswers+=(tmpl)
                wa+=1
    
    
        
            
    if kwargs["result_flag"]:
        print("l'exercici peta? ",kwargs["result_flag"])
        return CheckerResult(
            count == len(judge_lines), point_value * (1.0 * count / len(judge_lines)), None, strinput+"\u2719"+str(wronganswers)+ "\u2719"+str(len(judge_input))+ "\u2719"+str(len(judge_lines))   )
    elif count!=len(judge_lines) and senseEspaisJ==senseEspaisP:
        print("sembla que es tema d'espais")
        return CheckerResult(
            True, point_value, "Resposta correcta, pero fas malament espais o salts de linea.")
    elif count!=len(judge_lines) and lowerJ==lowerP:
        print("sembla que es tema de majus")
        return CheckerResult(
            True, point_value, "Resposta correcta, pero tens alguna majuscula malament.")
    elif processLines > judgeLines:
        return CheckerResult(
            count == len(judge_lines), point_value * (1.0 * count / len(judge_lines)), "imprimeixes MÉS línies de les esperades.", strinput+"\u2719"+str(wronganswers)+ "\u2719"+str(len(judge_input))+ "\u2719"+str(len(judge_lines))   )
    #Error, so keep the executor exception info
    elif processLines < judgeLines:
        return CheckerResult(
            count == len(judge_lines), point_value * (1.0 * count / len(judge_lines)), "imprimeixes MENYS línies de les esperades.", strinput+"\u2719"+str(wronganswers)+ "\u2719"+str(len(judge_input))+ "\u2719"+str(len(judge_lines))   )
    #Not an error, so use the new ifno
    else:
        print("L'exercici passa els casos de prova")
        if(count==len(judge_lines)):
            print("AC")
            percent = int((count)*100/(len(judge_lines)))
            return CheckerResult(
            True, point_value * (1.0 * count / len(judge_lines)), str(percent)+"%" if feedback else ""   )
        else:
            print("WA")

            percent = int((count)*100/(len(judge_lines)))
            return CheckerResult(
                count == len(judge_lines), point_value * (1.0 * count / len(judge_lines)), str(percent)+"%" if feedback else "", strinput+"\u2719"+str(wronganswers)+ "\u2719"+str(len(judge_input))+ "\u2719"+str(len(judge_lines))   )
    
check.run_on_error = True  # type: ignore 
